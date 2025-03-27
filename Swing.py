import os, re, logging, asyncio, time
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from tradingview_ta import TA_Handler, Interval
from keep_alive import keep_alive  # Keep-alive to prevent sleeping

# تحميل المتغيرات من ملف .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID"))

# إعداد السجل
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# تعريف الرموز
forex_symbols = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"]
cfd_symbols = ["XAUUSD", "US30USD"]

# إعداد إعدادات الفاحص والمنصة لكل رمز
config = {}
for sym in forex_symbols:
    config[sym] = {"screener": "forex", "exchange": "FOREXCOM"}
for sym in cfd_symbols:
    config[sym] = {"screener": "cfd", "exchange": "OANDA"}

# نستخدم الإطارات الزمنية: 4H و 1D
timeframes = {
    "4H": Interval.INTERVAL_4_HOURS,
    "1D": Interval.INTERVAL_1_DAY
}

# التخزين الداخلي للبيانات: لكل رمز ولكل إطار نخزن آخر 12 شمعة، حالة pending swing، وآخر دقيقة تمت معالجتها
price_data = {}
for sym in config:
    price_data[sym] = {}
    for tf in timeframes:
        price_data[sym][tf] = {
            "last_candles": [],
            "last_alert_time": None,
            "pending_swing": None,
            "last_processed_minute": None
        }

# (يمكن استخدام المشتركين للذكَر إذا رغبت)
subscribers_4H = set()
subscribers_1D = set()

# معرفات رسائل الاشتراك (سيتم إرسالها عند بدء التشغيل)
subscription_message_ids = {"4H": None, "1D": None}

# ------------------------------
# دالة detect_swing: الكشف عن السوينغ الأساسي باستخدام آخر 3 شموع
# ------------------------------
def detect_swing(candles):
    if len(candles) < 3:
        logger.debug("Not enough candles to detect swing.")
        return None
    previous = candles[-3]
    candidate = candles[-2]
    nxt = candles[-1]
    logger.debug(f"Detecting swing: previous={previous}, candidate={candidate}, next={nxt}")
    
    swing_type = None
    if candidate["high"] > previous["high"] and candidate["high"] > nxt["high"] and nxt["low"] <= candidate["low"]:
        swing_type = "HIGH"
    elif candidate["low"] < previous["low"] and candidate["low"] < nxt["low"] and nxt["high"] >= candidate["high"]:
        swing_type = "LOW"
    else:
        logger.debug("No swing detected based on the current candles.")
        return None

    detected = {
        "swing_type": swing_type,
        "formation": "BASIC",
        "candle_time": candidate["time"]
    }
    logger.debug(f"Swing detected: {detected}")
    return detected

# ------------------------------
# إعداد البوت الخاص بـ Discord
# ------------------------------
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

# ------------------------------
# on_ready: إرسال رسائل الاشتراك وتشغيل المهام
# ------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
    if alert_channel:
        # إرسال رسائل الاشتراك دائمًا عند بدء التشغيل
        msg_4h = await alert_channel.send("مهتم ب 4H سوينغ\nاضغط على الرياكشن للتسجيل.")
        subscription_message_ids["4H"] = msg_4h.id
        msg_1d = await alert_channel.send("مهتم ب 1D سوينغ\nاضغط على الرياكشن للتسجيل.")
        subscription_message_ids["1D"] = msg_1d.id
        logger.info("Subscription messages sent.")
    else:
        logger.error("Alert channel not found.")

    update_prices.start()
    heartbeat.start()

# ------------------------------
# on_raw_reaction_add: تتبع المشتركين
# ------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    if payload.message_id == subscription_message_ids.get("4H"):
        subscribers_4H.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 4H subscribers.")
    elif payload.message_id == subscription_message_ids.get("1D"):
        subscribers_1D.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 1D subscribers.")

# ------------------------------
# update_prices: المهمة الرئيسية
# تُنفذ كل ثانية ولكن تُعالج البيانات فقط عندما يكون الوقت مطابقًا للوقت المطلوب:
# - بالنسبة لإطار 4H: سيتم جلب البيانات عند 13:59:45، 17:59:45، 21:59:45، 01:59:45، 05:59:45، و09:59:45.
# - بالنسبة لإطار 1D: سيتم جلب البيانات عند 16:59:45.
# ------------------------------
@tasks.loop(seconds=1)
async def update_prices():
    current_time = datetime.now()
    logger.debug(f"Current time: {current_time.strftime('%H:%M:%S')}")
    
    # تحديد أوقات الجلب لإطار 4H (حسب توقيت GMT+1 / UTC+1)
    target_times_4H = [(13, 59, 45), (17, 59, 45), (21, 59, 45), (1, 59, 45), (5, 59, 45), (9, 59, 45)]
    trigger_4H = any(
        current_time.hour == th and current_time.minute == tm and current_time.second == ts 
        for (th, tm, ts) in target_times_4H
    )
    
    # بالنسبة للإطار اليومي (1D): سيتم الجلب عند 16:59:45 (قبل 15 ثانية من الساعة 17)
    trigger_1D = (current_time.hour == 16 and current_time.minute == 59 and current_time.second == 45)
    
    # معالجة إطار 4H
    if trigger_4H:
        logger.debug("Trigger for 4H detected.")
        for symbol, cfg in config.items():
            try:
                data = price_data[symbol]["4H"]
                if data["last_processed_minute"] == current_time.minute:
                    logger.debug(f"Already processed 4H for {symbol} for minute {current_time.minute}.")
                    continue

                handler = TA_Handler(
                    symbol=symbol,
                    screener=cfg["screener"],
                    exchange=cfg["exchange"],
                    interval=timeframes["4H"]
                )
                analysis = handler.get_analysis()
                indicators = analysis.indicators

                new_candle = {
                    "high": float(indicators.get("high", 0)),
                    "low": float(indicators.get("low", 0)),
                    "close": float(indicators.get("close", 0)),
                    "time": current_time
                }
                data["last_candles"].append(new_candle)
                if len(data["last_candles"]) > 12:
                    data["last_candles"].pop(0)
                data["last_processed_minute"] = current_time.minute
                logger.info(f"New 4H candle for {symbol}: {new_candle}")

                if data["pending_swing"] is None:
                    swing = detect_swing(data["last_candles"])
                    if swing is not None:
                        logger.info(f"Basic 4H swing detected for {symbol}: {swing}")
                        await send_alert(symbol, "4H", swing)
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
                        logger.info(f"Extended pending 4H swing for {symbol}: chain_length={pending['chain_length']}")
                        await send_alert(symbol, "4H", {
                            "swing_type": pending["swing_type"],
                            "formation": "SEQUENCE",
                            "candle_time": pending["base_time"]
                        })
                    else:
                        logger.info(f"Pending 4H swing for {symbol} broken.")
                        data["pending_swing"] = None
            except Exception as e:
                logger.error(f"Error fetching 4H data for {symbol}: {e}")

    # معالجة إطار 1D
    if trigger_1D:
        logger.debug("Trigger for 1D detected.")
        for symbol, cfg in config.items():
            try:
                data = price_data[symbol]["1D"]
                if data["last_processed_minute"] == current_time.minute:
                    logger.debug(f"Already processed 1D for {symbol} for minute {current_time.minute}.")
                    continue

                handler = TA_Handler(
                    symbol=symbol,
                    screener=cfg["screener"],
                    exchange=cfg["exchange"],
                    interval=timeframes["1D"]
                )
                analysis = handler.get_analysis()
                indicators = analysis.indicators

                new_candle = {
                    "high": float(indicators.get("high", 0)),
                    "low": float(indicators.get("low", 0)),
                    "close": float(indicators.get("close", 0)),
                    "time": current_time
                }
                data["last_candles"].append(new_candle)
                if len(data["last_candles"]) > 12:
                    data["last_candles"].pop(0)
                data["last_processed_minute"] = current_time.minute
                logger.info(f"New 1D candle for {symbol}: {new_candle}")

                if data["pending_swing"] is None:
                    swing = detect_swing(data["last_candles"])
                    if swing is not None:
                        logger.info(f"Basic 1D swing detected for {symbol}: {swing}")
                        await send_alert(symbol, "1D", swing)
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
                        logger.info(f"Extended pending 1D swing for {symbol}: chain_length={pending['chain_length']}")
                        await send_alert(symbol, "1D", {
                            "swing_type": pending["swing_type"],
                            "formation": "SEQUENCE",
                            "candle_time": pending["base_time"]
                        })
                    else:
                        logger.info(f"Pending 1D swing for {symbol} broken.")
                        data["pending_swing"] = None
            except Exception as e:
                logger.error(f"Error fetching 1D data for {symbol}: {e}")

    await asyncio.sleep(1)

# ------------------------------
# heartbeat: تسجيل رسالة كل دقيقة لتأكيد عمل البوت
# ------------------------------
@tasks.loop(seconds=60)
async def heartbeat():
    logger.info("Heartbeat: Bot is running...")

# ------------------------------
# send_alert: إرسال التنبيه بتنسيق Symbol/Timeframe/Type
# ------------------------------
async def send_alert(symbol, timeframe, swing_result):
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        logger.error("Alert channel not found when trying to send alert.")
        return
    swing_type = swing_result["swing_type"]
    formation = swing_result.get("formation", "BASIC")
    if formation == "BASIC":
        type_text = f"Swing {'High' if swing_type=='HIGH' else 'Low'}"
    else:
        type_text = f"Sequence {'High' if swing_type=='HIGH' else 'Low'}"
    content = f"{symbol}/{timeframe}/{type_text}"
    await channel.send(content)
    logger.info(f"Alert sent: {content}")

# ------------------------------
# Keep the bot alive using Flask
# ------------------------------
keep_alive()

# ------------------------------
# Run the bot
# ------------------------------
bot.run(TOKEN)
