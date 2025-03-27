import os, re, logging, asyncio, time
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from tradingview_ta import TA_Handler, Interval
from keep_alive import keep_alive  # استيراد دالة البقاء نشط

# تحميل المتغيرات من ملف .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID"))

# إعداد السجل
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# إعداد الرموز
forex_symbols = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"]
cfd_symbols = ["XAUUSD", "US30USD"]

# إعداد الكونفيغ لكل رمز (screener و exchange)
config = {}
for sym in forex_symbols:
    config[sym] = {"screener": "forex", "exchange": "FOREXCOM"}
for sym in cfd_symbols:
    config[sym] = {"screener": "cfd", "exchange": "OANDA"}

# الإطارات الزمنية: 4H و 1D
timeframes = {
    "4H": Interval.INTERVAL_4_HOURS,
    "1D": Interval.INTERVAL_1_DAY
}

# تخزين بيانات الشموع لكل رمز ولكل فريم
price_data = {}
for sym in config:
    price_data[sym] = {}
    for tf in timeframes:
        price_data[sym][tf] = {"last_candles": [], "last_alert_time": None, "pending_swing": None}

# مجموعات المشتركين في التنبيهات
subscribers_4h = set()
subscribers_1d = set()
subscription_message_ids = {"4H": None, "1D": None}

# -----------------------------------------
# دالة اكتشاف السوينغ الأساسي (Basic Swing)
# -----------------------------------------
def detect_swing(candles):
    if len(candles) < 3:
        return None
    previous = candles[-3]
    candidate = candles[-2]
    nxt = candles[-1]

    swing_type = None
    if candidate["high"] > previous["high"] and candidate["high"] > nxt["high"] and nxt["low"] <= candidate["low"]:
        swing_type = "HIGH"
    elif candidate["low"] < previous["low"] and candidate["low"] < nxt["low"] and nxt["high"] >= candidate["high"]:
        swing_type = "LOW"
    else:
        return None

    return {
        "swing_type": swing_type,
        "formation": "BASIC",
        "candle_time": candidate["time"]
    }

# -----------------------------------------
# إعداد البوت
# -----------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents, help_command=None)
    async def setup_hook(self):
        logger.info("Bot setup completed.")

bot = MyBot()

# -----------------------------------------
# حدث on_ready
# -----------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel:
        msg_4h = await channel.send("مهتم ب 4H سوينغ\nاضغط على الرياكشن للتسجيل.")
        msg_1d = await channel.send("مهتم ب 1D سوينغ\nاضغط على الرياكشن للتسجيل.")
        subscription_message_ids["4H"] = msg_4h.id
        subscription_message_ids["1D"] = msg_1d.id
        logger.info("Subscription messages sent.")
    update_prices.start()

# -----------------------------------------
# تسجيل التفاعلات على رسائل الاشتراك
# -----------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    if payload.message_id == subscription_message_ids.get("4H"):
        subscribers_4h.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 4H subscribers.")
    elif payload.message_id == subscription_message_ids.get("1D"):
        subscribers_1d.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 1D subscribers.")

# -----------------------------------------
# المهمة الدورية لتحديث الأسعار وفحص السوينغ والتسلسل
# -----------------------------------------
@tasks.loop(seconds=60)
async def update_prices():
    logger.debug("Checking prices for all symbols and timeframes...")
    for symbol, cfg in config.items():
        for tf_label, tf_interval in timeframes.items():
            try:
                handler = TA_Handler(
                    symbol=symbol,
                    screener=cfg["screener"],
                    exchange=cfg["exchange"],
                    interval=tf_interval
                )
                analysis = handler.get_analysis()
                indicators = analysis.indicators

                candle_time = analysis.time if hasattr(analysis, "time") else int(time.time())
                high = float(indicators.get("high", 0))
                low = float(indicators.get("low", 0))
                close = float(indicators.get("close", 0))

                data = price_data[symbol][tf_label]
                if not data["last_candles"] or data["last_candles"][-1]["time"] != candle_time:
                    new_candle = {"high": high, "low": low, "close": close, "time": candle_time}
                    data["last_candles"].append(new_candle)
                    if len(data["last_candles"]) > 12:
                        data["last_candles"].pop(0)
                    logger.info(f"New candle for {symbol} {tf_label}: {new_candle}")

                    # إذا لم يكن هناك pending swing، اكتشف سوينغ أساسي
                    if data["pending_swing"] is None:
                        swing = detect_swing(data["last_candles"])
                        if swing is not None:
                            await send_alert(symbol, tf_label, swing)
                            data["pending_swing"] = {
                                "swing_type": swing["swing_type"],
                                "base_time": swing["candle_time"],
                                "chain_length": 1,
                                "reference": data["last_candles"][-2]
                            }
                    else:
                        pending = data["pending_swing"]
                        ref = pending["reference"]
                        if pending["swing_type"] == "HIGH":
                            condition = (new_candle["close"] < ref["low"]) and (new_candle["high"] <= ref["high"])
                        else:
                            condition = (new_candle["close"] > ref["high"]) and (new_candle["low"] >= ref["low"])
                        if condition:
                            pending["chain_length"] += 1
                            pending["reference"] = new_candle
                            logger.info(f"Extended pending swing for {symbol} {tf_label}: chain_length={pending['chain_length']}")
                            await send_alert(symbol, tf_label, {
                                "swing_type": pending["swing_type"],
                                "formation": "SEQUENCE",
                                "candle_time": pending["base_time"]
                            })
                        else:
                            logger.info(f"Pending swing for {symbol} {tf_label} broken.")
                            data["pending_swing"] = None
                else:
                    logger.debug(f"No new candle for {symbol} {tf_label}.")
            except Exception as e:
                logger.error(f"Error fetching data for {symbol} ({tf_label}): {e}")
    await asyncio.sleep(1)

# -----------------------------------------
# دالة إرسال التنبيه مع تاغ المشتركين المناسبين
# -----------------------------------------
async def send_alert(symbol, timeframe, swing_result):
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return
    swing_type = swing_result["swing_type"]
    formation = swing_result.get("formation", "BASIC")
    content = (f"تنبيه: تم تشكيل {formation} {swing_type} سوينغ للعملة {symbol} على فريم {timeframe}.\n"
               f"الشمعة (Base): {datetime.fromtimestamp(swing_result['candle_time']).strftime('%Y-%m-%d %H:%M:%S')}")
    if timeframe == "4H" and subscribers_4h:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_4h)
        content += f"\n{mentions}"
    elif timeframe == "1D" and subscribers_1D:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_1d)
        content += f"\n{mentions}"
    await channel.send(content)
    logger.info(f"Alert sent for {symbol} on {timeframe} with formation {formation}.")

# -----------------------------------------
# تشغيل keep_alive لتجنب توقف التطبيق
# -----------------------------------------
keep_alive()

# -----------------------------------------
# تشغيل البوت
# -----------------------------------------
bot.run(TOKEN)
