import os, re, logging, asyncio, time
from datetime import datetime
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from tradingview_ta import TA_Handler, Interval
from keep_alive import keep_alive  # دالة keep_alive لتشغيل خادم ويب بسيط

# تحميل المتغيرات من ملف .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID"))

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define symbols
forex_symbols = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"]
cfd_symbols = ["XAUUSD", "US30USD"]

# Screener & Exchange configuration
config = {}
for sym in forex_symbols:
    config[sym] = {"screener": "forex", "exchange": "FOREXCOM"}
for sym in cfd_symbols:
    config[sym] = {"screener": "cfd", "exchange": "OANDA"}

# Timeframes
timeframes = {
    "4H": Interval.INTERVAL_4_HOURS,
    "1D": Interval.INTERVAL_1_DAY
}

# Storage for candle data and swing state in memory
price_data = {}
for sym in config:
    price_data[sym] = {}
    for tf in timeframes:
        # يحتفظ بآخر 12 شمعة وحالة pending swing
        price_data[sym][tf] = {"last_candles": [], "last_alert_time": None, "pending_swing": None}

# Subscribers for alerts
subscribers_4h = set()
subscribers_1d = set()

# Subscription message IDs for 4H and 1D alerts
subscription_message_ids = {"4H": None, "1D": None}

# ------------------------------
# detect_swing: Basic Swing Logic
# ------------------------------
def detect_swing(candles):
    if len(candles) < 3:
        return None
    previous = candles[-3]
    candidate = candles[-2]
    nxt = candles[-1]

    swing_type = None
    # تحقق من Swing High
    if (candidate["high"] > previous["high"] and 
        candidate["high"] > nxt["high"] and 
        nxt["low"] <= candidate["low"]):
        swing_type = "HIGH"
    # تحقق من Swing Low
    elif (candidate["low"] < previous["low"] and 
          candidate["low"] < nxt["low"] and 
          nxt["high"] >= candidate["high"]):
        swing_type = "LOW"
    else:
        return None

    return {
        "swing_type": swing_type,
        "formation": "BASIC",
        "candle_time": candidate["time"]
    }

# ------------------------------
# Discord Bot Setup
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
# on_ready: Restore subscription messages and start tasks
# ------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
    if alert_channel:
        # استرجاع رسائل الاشتراك (آخر 50 رسالة)
        async for msg in alert_channel.history(limit=50):
            if msg.author.id == bot.user.id:
                if msg.content.startswith("مهتم ب 4H سوينغ"):
                    subscription_message_ids["4H"] = msg.id
                    for reaction in msg.reactions:
                        users = [user async for user in reaction.users()]
                        for u in users:
                            if u.id != bot.user.id:
                                subscribers_4h.add(u.id)
                elif msg.content.startswith("مهتم ب 1D سوينغ"):
                    subscription_message_ids["1D"] = msg.id
                    for reaction in msg.reactions:
                        users = [user async for user in reaction.users()]
                        for u in users:
                            if u.id != bot.user.id:
                                subscribers_1d.add(u.id)
        # إنشاء رسائل الاشتراك إذا لم تكن موجودة
        if not subscription_message_ids["4H"]:
            msg_4h = await alert_channel.send("مهتم ب 4H سوينغ\nاضغط على الرياكشن للتسجيل.")
            subscription_message_ids["4H"] = msg_4h.id
        if not subscription_message_ids["1D"]:
            msg_1d = await alert_channel.send("مهتم ب 1D سوينغ\nاضغط على الرياكشن للتسجيل.")
            subscription_message_ids["1D"] = msg_1d.id
        logger.info("Subscription messages ready.")
    else:
        logger.error("Alert channel not found.")

    update_prices.start()
    heartbeat.start()

# ------------------------------
# on_raw_reaction_add: Track subscribers
# ------------------------------
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

# ------------------------------
# update_prices: Main loop every 10 minutes (600 seconds)
# ------------------------------
@tasks.loop(seconds=600)
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

                    # Check for pending swing or detect a new basic swing
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

# ------------------------------
# heartbeat: A simple task to log that the bot is alive every minute
# ------------------------------
@tasks.loop(seconds=60)
async def heartbeat():
    logger.info("Heartbeat: Bot is running...")

# ------------------------------
# send_alert: Send Discord alert
# ------------------------------
async def send_alert(symbol, timeframe, swing_result):
    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return
    swing_type = swing_result["swing_type"]
    formation = swing_result.get("formation", "BASIC")
    content = (
        f"تنبيه: تم تشكيل {formation} {swing_type} سوينغ للعملة {symbol} على فريم {timeframe}.\n"
        f"الشمعة (Base): {datetime.fromtimestamp(swing_result['candle_time']).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if timeframe == "4H" and subscribers_4h:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_4h)
        content += f"\n{mentions}"
    elif timeframe == "1D" and subscribers_1d:
        mentions = " ".join(f"<@{uid}>" for uid in subscribers_1d)
        content += f"\n{mentions}"
    await channel.send(content)
    logger.info(f"Alert sent for {symbol} on {timeframe} with formation {formation}.")

# ------------------------------
# Keep the bot alive
# ------------------------------
keep_alive()

# ------------------------------
# Run the bot
# ------------------------------
bot.run(TOKEN)
