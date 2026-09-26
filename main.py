import os
import sys
import html
import asyncio
import logging
from collections import deque
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events

from ai_rewriter import GeminiRewriter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HoPokievuBot")

# Load environment variables
load_dotenv()

# --- Telegram API & Client ---
api_id = int(os.getenv("TG_API_ID", 0))
api_hash = os.getenv("TG_API_HASH", "")
phone = os.getenv("TG_PHONE", "")

# --- Bot & Chat ---
bot_token = os.getenv("BOT_TOKEN", "")
target_chat_id = int(os.getenv("TARGET_CHAT_ID", 0))

# --- Channels ---
source_monitor_channel = os.getenv("SOURCE_MONITOR_CHANNEL", "kyiv_monitor1")
source_alert_channel = os.getenv("SOURCE_ALERT_CHANNEL", "UkraineAlarmSignal")

# --- Watermark ---
watermark_url = os.getenv("WATERMARK_URL", "http://t.me/+r6sIdYUpBrwzNjMy")
watermark_text = os.getenv("WATERMARK_TEXT", "Що по Києву?👉 Підписатись✅")

# --- AI Configuration ---
gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY", "")
gemini_model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")

# --- Kyiv & Regional Markers for UkraineAlarmSignal ---
KYIV_MARKERS = [
    "київ", "київськ", "білоцерківськ", "бориспільськ", 
    "броварськ", "бучанськ", "вишгородськ", "обухівськ", "фастівськ"
]

OTHER_OBLAST_MARKERS = [
    "сумськ", "миколаївськ", "одеськ", "харківськ", "дніпро", 
    "полтавськ", "чернігівськ", "черкаськ", "вінницьк", "житомирськ", 
    "запорізьк", "херсонськ", "кіровоградськ", "донецьк", "луганськ", 
    "львівськ", "волинськ", "рівненськ", "тернопільськ", "хмельницьк", 
    "івано-франківськ", "закарпатськ", "чернівецьк", "крим", "севастополь"
]

geo_keywords = [
    "Київ", "Києві", "Дарниця", "Позняки", "Троєщина", "Лісовий масив", "Лівобережний масив",
    "Воскресенка", "Русанівка", "Березняки", "Шулявка", "Святошин", "Нивки", "Борщагівка",
    "Голосіїв", "Теремки", "Оболонь", "Деснянський район", "Поділ", "Хрещатик",
    "Бровари", "Вишгород", "Ірпінь", "Біла Церква", "Обухів", "Фастів", "Київська область", "ТЕЦ-5", "ТЕЦ-6"
]

military_keywords = [
    "шахед", "дрон", "бпла", "ракета", "удар", "атака", "вибух", "влучання",
    "ППО", "кінджал", "герань", "миг", "пуск", "летить", "ціль", "груп ракет", "група ракет", "группы ракет", "ракет", "группа", "группи"
]

exclude_keywords = [
    "сподіваюсь", "вижили", "залишайтесь", "було", "була", "були",
    "минуло", "не підтверджено", "допомога", "Полтави", "Полтавщини", "Полтава", "ЧЕРНІГОВА", "Чернігова"
]

# Client & State
client = TelegramClient("session_combined", api_id, api_hash)
ai_rewriter: Optional[GeminiRewriter] = GeminiRewriter(gemini_key, model=gemini_model) if gemini_key else None
http_session: Optional[aiohttp.ClientSession] = None
processed_hashes = deque(maxlen=200)


def format_message(body: str) -> str:
    """Escapes HTML entities safely in text and appends formatted watermark."""
    escaped_body = html.escape(body.strip())
    watermark_html = f'\n\n<a href="{watermark_url}">{html.escape(watermark_text)}</a>'
    return f"{escaped_body}{watermark_html}"


async def send_message_via_bot(text: str) -> bool:
    """Sends message to target Telegram channel asynchronously with error handling."""
    if not http_session or http_session.closed:
        logger.error("HTTP session is not initialized or closed.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        async with http_session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("ok"):
                return True
            else:
                logger.error(f"Telegram Bot API error: {data}")
                return False
    except Exception as e:
        logger.error(f"Failed to send message via bot: {e}")
        return False


def filter_alarm_message(text: str) -> Optional[str]:
    """
    Filters alarm message specifically for Kyiv and Kyiv region.
    Supports single-region alerts ('🟡 Київ') and multi-district lists
    ('• Броварський район (Київська обл.)\n• Вознесенський район (Миколаївська обл.)').
    Filters out other oblasts while keeping headers and Kyiv districts.
    """
    if not text or not text.strip():
        return None

    low_text = text.lower()
    has_kyiv = any(m in low_text for m in KYIV_MARKERS)
    has_other_obl = any(m in low_text for m in OTHER_OBLAST_MARKERS)

    # If message doesn't mention Kyiv or Kyiv districts at all
    if not has_kyiv:
        return None

    # If it is exclusively about Kyiv / Kyiv Oblast without other oblasts mentioned
    if not has_other_obl:
        return text.strip()

    # Multi-oblast message: filter line-by-line
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    kept_lines = []
    has_kept_kyiv_district = False

    for line in lines:
        low_line = line.lower()
        is_other_obl = any(m in low_line for m in OTHER_OBLAST_MARKERS)
        is_kyiv = any(m in low_line for m in KYIV_MARKERS)

        if is_kyiv and not is_other_obl:
            kept_lines.append(line)
            has_kept_kyiv_district = True
        elif not is_other_obl and not is_kyiv:
            # Header or status note line (e.g. '🟡 Жовтий рівень тривоги', 'Прямуйте в укриття!')
            kept_lines.append(line)

    if not has_kept_kyiv_district:
        return None

    return "\n".join(kept_lines)


def fallback_filter_monitor(text: str) -> str:
    """Backup keyword filter in case AI service is temporarily unavailable."""
    result = []
    for line in text.splitlines():
        lower = line.lower()
        if any(bad in lower for bad in exclude_keywords):
            continue
        if any(g.lower() in lower for g in geo_keywords) and any(m.lower() in lower for m in military_keywords):
            result.append(line)
    return "\n".join(result)


# --- Handler for @UkraineAlarmSignal (Immediate Alerts) ---
@client.on(events.NewMessage(chats=source_alert_channel))
async def alert_handler(event):
    msg = event.message.message or ""
    if not msg or not msg.strip():
        return

    filtered_alert = filter_alarm_message(msg)
    if filtered_alert:
        full_msg = format_message(filtered_alert)
        logger.info(f"[🚨 ТРИВОГА/ВІДБІЙ] =>\n{filtered_alert}")
        await send_message_via_bot(full_msg)
    else:
        logger.info(f"[ALERT SKIPPED] Non-Kyiv alarm: {msg[:60]}...")


# --- Handler for Monitor Channel (AI-driven rewrite) ---
@client.on(events.NewMessage(chats=source_monitor_channel))
async def monitor_handler(event):
    msg = event.message.message or ""
    if not msg or not msg.strip():
        return

    # Deduplication check
    msg_hash = hash(msg.strip())
    if msg_hash in processed_hashes:
        return
    processed_hashes.append(msg_hash)

    logger.info(f"[📡 RAW MONITOR] Incoming message: {msg[:60]}...")

    rewritten_text = None
    status = "ERROR"

    # Step 1: AI Analysis & Rewrite
    if ai_rewriter and http_session:
        rewritten_text, status = await ai_rewriter.rewrite_post(http_session, msg)

    # Step 2: Handle results
    if status == "SUCCESS" and rewritten_text:
        full_msg = format_message(rewritten_text)
        logger.info(f"[✅ AI APPROVED & PUBLISHED] =>\n{rewritten_text}")
        await send_message_via_bot(full_msg)
    elif status == "IRRELEVANT":
        logger.info("[FILTERED OUT] Post skipped by AI (not relevant to Kyiv / spam).")
    else:
        # Fallback to keyword filter if AI experienced an error/timeout or is not configured
        filtered = fallback_filter_monitor(msg)
        if filtered.strip():
            logger.warning("[FALLBACK ACTIVATED] Publishing keyword-filtered message due to AI unavailability.")
            full_msg = format_message(filtered)
            await send_message_via_bot(full_msg)
        else:
            logger.info("[FILTERED OUT] Post skipped by fallback filter.")


# --- Main entry point ---
async def main():
    global http_session

    if not api_id or not api_hash or not bot_token:
        logger.error("Configuration missing! Please verify TG_API_ID, TG_API_HASH and BOT_TOKEN in .env.")
        return

    # Create persistent async HTTP session
    http_session = aiohttp.ClientSession()

    try:
        await client.start(phone)
        logger.info("=" * 50)
        logger.info("✅ Бот успішно запущено!")
        logger.info(f"📡 Моніторинг каналів: @{source_monitor_channel}, @{source_alert_channel}")
        logger.info(f"🤖 ШІ рерайтер: {'АКТИВОВАНО (Gemini ' + gemini_model + ')' if ai_rewriter else 'ВІМКНЕНО'}")
        logger.info(f"🎯 Цільовий чат ID: {target_chat_id}")
        logger.info("=" * 50)

        await client.run_until_disconnected()
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Бот зупинено вручну.")
