import os, re, logging, asyncio, time
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from tradingview_ta import TA_Handler, Interval
from keep_alive import keep_alive  # استخدم هذا إذا كنت تستخدم خدمة تشغيل مستمرة

# تحميل المتغيرات من ملف .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID"))

# إعداد السجل
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# إعداد الرموز
# المجموعة الأولى (Forex)
forex_symbols = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"]
# المجموعة الثانية (CFD)
cfd_symbols = ["XAUUSD", "US30USD"]

# إعداد الكونفيغ لكل رمز: لكل رمز نحدد الـ screener والـ exchange المناسبين
config = {}
for sym in forex_symbols:
    config[sym] = {"screener": "forex", "exchange": "FOREXCOM"}
for sym in cfd_symbols:
    config[sym] = {"screener": "cfd", "exchange": "OANDA"}

# الإطارات الزمنية التي نتابعها: 4 ساعات و1 يوم
timeframes = {
    "4H": Interval.INTERVAL_4_HOURS,
    "1D": Interval.INTERVAL_1_DAY
}

# تخزين بيانات الشموع لكل رمز ولكل إطار زمني
# الهيكل: price_data[symbol][timeframe] = { "last_candles": [ { "high": ..., "low": ..., "close": ..., "time": ... }, ... ], "last_alert_time": None }
price_data = {}
for sym in config:
    price_data[sym] = {}
    for tf in timeframes:
        price_data[sym][tf] = {"last_candles": [], "last_alert_time": None}

# مجموعات المشتركين في التنبيهات بحسب الفريم
subscribers_4h = set()
subscribers_1d = set()
# حفظ معرفات رسائل الاشتراك (4H و1D)
subscription_message_ids = {"4H": None, "1D": None}

# -----------------------------------------
# دالة اكتشاف السوينغ (Basic & Sequence)
# -----------------------------------------
def detect_swing(candles):
    """
    يعتمد اكتشاف السوينغ على آخر 3 شموع:
      - candidate = الشمعة قبل الأخيرة (candles[-2])
      - previous = الشمعة قبل candidate (candles[-3])
      - next_candle = آخر شمعة (candles[-1])
    الشروط:
      BASIC Swing High:
         candidate["high"] > previous["high"] و candidate["high"] > next_candle["high"]
         و next_candle["low"] <= candidate["low"]
      BASIC Swing Low:
         candidate["low"] < previous["low"] و candidate["low"] < next_candle["low"]
         و next_candle["high"] >= candidate["high"]
    كما يتم التحقق من شروط السلسلة (Sequence) باستخدام:
      لـ HIGH: (next_candle["close"] < previous["low"] و next_candle["high"] <= previous["high"])
      لـ LOW: (next_candle["close"] > previous["high"] و next_candle["low"] >= previous["low"])
    """
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

    formation = "BASIC"
    # التحقق من شروط السلسلة (Sequence)
    if swing_type == "HIGH":
        if nxt["close"] < previous["low"] and nxt["high"] <= previous["high"]:
            formation = "SEQUENCE"
    else:
        if nxt["close"] > previous["high"] and nxt["low"] >= previous["low"]:
            formation = "SEQUENCE"

    return {
        "swing_type": swing_type,
        "formation": formation,
        "candle_time": candidate["time"]  # وقت الشمعة التي تمثل السوينغ
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
# حدث on_ready: عند بدء تشغيل البوت
# -----------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel:
        # إرسال رسالتين للاشتراك في التنبيهات (4H و1D)
        msg_4h = await channel.send("مهتم ب 4H سوينغ\nاضغط على الرياكشن للتسجيل.")
        msg_1d = await channel.send("مهتم ب 1D سوينغ\nاضغط على الرياكشن للتسجيل.")
        subscription_message_ids["4H"] = msg_4h.id
        subscription_message_ids["1D"] = msg_1d.id
        logger.info("Subscription messages sent.")
    # بدء مهمة تحديث الأسعار
    update_prices.start()

# -----------------------------------------
# حدث on_raw_reaction_add: لتسجيل التفاعل على رسائل الاشتراك
# -----------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    if payload.user_id == bot.user.id:
        return
    # التأكد من أن الرياكشن على إحدى رسائل الاشتراك
    if payload.message_id == subscription_message_ids.get("4H"):
        subscribers_4h.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 4H subscribers.")
    elif payload.message_id == subscription_message_ids.get("1D"):
        subscribers_1d.add(payload.user_id)
        logger.info(f"Added user {payload.user_id} to 1D subscribers.")

# -----------------------------------------
# المهمة الدورية لتحديث الأسعار وفحص السوينغ
# -----------------------------------------
@tasks.loop(seconds=3600)  # يتم الفحص كل ساعة (3600 ثانية)
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

                # الحصول على وقت الشمعة؛ إذا لم يكن متوفرًا، نستخدم الوقت الحالي
                candle_time = analysis.time if hasattr(analysis, "time") else int(time.time())
                high = float(indicators.get("high", 0))
                low = float(indicators.get("low", 0))
                close = float(indicators.get("close", 0))

                data = price_data[symbol][tf_label]
                # إذا لم تتغير بيانات الشمعة (أي لا توجد شمعة جديدة) لا نقوم بتسجيلها
                if not data["last_candles"] or data["last_candles"][-1]["time"] != candle_time:
                    new_candle = {"high": high, "low": low, "close": close, "time": candle_time}
                    data["last_candles"].append(new_candle)
                    if len(data["last_candles"]) > 12:
                        data["last_candles"].pop(0)
                    logger.info(f"New candle for {symbol} {tf_label}: {new_candle}")
                    
                    # فحص تكوين سوينغ/سلسلة بعد إغلاق الشمعة
                    swing_result = detect_swing(data["last_candles"])
                    if swing_result is not None:
                        # تأكد من عدم تكرار التنبيه لنفس الشمعة
                        if data["last_alert_time"] != swing_result["candle_time"]:
                            data["last_alert_time"] = swing_result["candle_time"]
                            await send_alert(symbol, tf_label, swing_result)
                else:
                    logger.debug(f"No new candle for {symbol} {tf_label}.")
            except Exception as e:
                logger.error(f"Error fetching data for {symbol} ({tf_label}): {e}")
    await asyncio.sleep(1)

# دالة إرسال التنبيه مع تاغ المشتركين المناسبين حسب الفريم
async def send_alert(symbol, timeframe, swing_result):
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return
    swing_type = swing_result["swing_type"]
    formation = swing_result["formation"]
    content = (f"تنبيه: تم تشكيل {formation} {swing_type} سوينغ للعملة {symbol} على فريم {timeframe}.\n"
               f"الشمعة: {datetime.fromtimestamp(swing_result['candle_time']).strftime('%Y-%m-%d %H:%M:%S')}")
    if timeframe == "4H" and subscribers_4h:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_4h)
        content += f"\n{mentions}"
    elif timeframe == "1D" and subscribers_1d:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_1d)
        content += f"\n{mentions}"
    await channel.send(content)
    logger.info(f"Alert sent for {symbol} on {timeframe}.")

# -----------------------------------------
# تشغيل خادم keep_alive إذا كنت تستخدمه
# -----------------------------------------
keep_alive()

# تشغيل البوت
bot.run(TOKEN)
