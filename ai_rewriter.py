import logging
import asyncio
import aiohttp
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ти — редактор оперативного Telegram-каналу новин 'Що по Києву?'.
Твоє завдання — переписувати чернетки повідомлень від @kyiv_monitor1 у ЖИВОМУ, ЕНЕРГІЙНОМУ, НЕОФІЦІЙНОМУ стилі швидкого телеграм-каналу.

ГОЛОВНІ ПРАВИЛА:
1. БЕЗ БЮРОКРАТІЇ ТА ОФІЦІОЗУ! Жодних нудних канцеляризмів ('Зафіксовано рух', 'повітряної цілі в регіоні', 'інформація уточнюється'). Пиши просто, коротко і швидко, як живий адмін оперативного каналу.
2. ЗАВЖДИ ЗБЕРІГАЙ ТОЧНУ КІЛЬКІСТЬ ЦІЛЕЙ (якщо вона є в оригіналі):
   - Якщо 3 ракети — пиши обов'язково '3 ракети'!
   - Якщо 4 ракети — пиши обов'язково '4 ракети'!
   - Якщо 2 БпЛА — '+2 нові БпЛА' або '2 шахеди'!
3. СТИЛЬ І ПРИКЛАДИ:
   - '3 ракети на Київ. 4 ракети падають.' -> '‼️ 3 ракети на Київ! Ще 4 ракети падають! В укриття!'
   - 'Нові 2 на Вишгородський.' -> '🛸 +2 нові на Вишгородський район!'
   - 'Перший збили, другий там же курсом. Підліт до Києва через 12-15 хвилин.' -> '💥 1 збили, другий тим же курсом! Підліт шахеда до Києва через ~15 хв ⏳'
   - 'є збиття.' -> '💥 Є збиття!'
   - 'Балістична ракета на Вишневе.' -> '🚀 Балістика на Вишневе! В укриття!'
   - До назв районів додавай слово 'район' ('на Вишгородський район').
4. ОЧИЩЕННЯ: Завжди повністю видаляй @kyiv_monitor1, будь-які посилання, рекламу чи спам.
5. ФІЛЬТРАЦІЯ: Якщо пост взагалі не про Київ чи область (а про інше місто) — пиши ТІЛЬКИ: IGNORE.
6. ВІДПОВІДЬ: Тільки готовий текст посту для публікації, без лапок чи коментарів."""


class GroqRewriter:
    def __init__(self, api_key: str, model: str = "qwen/qwen3.8-27b"):
        self.api_key = api_key
        self.model = model
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def rewrite_post(self, session: aiohttp.ClientSession, original_text: str) -> Tuple[Optional[str], str]:
        if not original_text or not original_text.strip():
            return None, "IRRELEVANT"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": original_text.strip()}
            ],
            "temperature": 0.1,
            "max_tokens": 150
        }

        for attempt in range(2):
            try:
                async with session.post(self.url, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 503 or resp.status == 429:
                        logger.warning(f"Groq API rate limit/busy (status {resp.status}), attempt {attempt + 1}")
                        if attempt == 0:
                            await asyncio.sleep(1)
                            continue
                        return None, "ERROR"

                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.warning(f"Groq API status {resp.status}: {err_text[:200]}")
                        return None, "ERROR"

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return None, "ERROR"

                    content = choices[0].get("message", {}).get("content", "").strip()
                    if not content:
                        return None, "ERROR"

                    if content.upper().startswith("IGNORE"):
                        logger.info("AI determined post is not relevant for Kyiv (IGNORE).")
                        return None, "IRRELEVANT"

                    return content, "SUCCESS"

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                err_name = type(e).__name__
                logger.warning(f"Groq API {err_name} on attempt {attempt + 1}")
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return None, "ERROR"
            except Exception as e:
                logger.error(f"Unexpected error calling Groq API: {e}", exc_info=True)
                return None, "ERROR"

        return None, "ERROR"


class GeminiRewriter:
    def __init__(self, api_key: str, model: str = "gemini-flash-lite-latest"):
        self.api_key = api_key
        self.model = model
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        self.headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }

    async def rewrite_post(self, session: aiohttp.ClientSession, original_text: str) -> Tuple[Optional[str], str]:
        if not original_text or not original_text.strip():
            return None, "IRRELEVANT"

        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": original_text.strip()}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 250
            }
        }

        for attempt in range(2):
            try:
                async with session.post(self.url, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 503 or resp.status == 429:
                        logger.warning(f"Gemini API rate limit/high demand (status {resp.status}), attempt {attempt + 1}")
                        if attempt == 0:
                            await asyncio.sleep(1)
                            continue
                        return None, "ERROR"

                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.warning(f"Gemini API status {resp.status}: {err_text[:200]}")
                        return None, "ERROR"

                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return None, "ERROR"

                    parts = candidates[0].get("content", {}).get("parts", [])
                    generated_text = "".join(p.get("text", "") for p in parts).strip()

                    if not generated_text:
                        return None, "ERROR"

                    if generated_text.upper().startswith("IGNORE"):
                        logger.info("AI determined post is not relevant for Kyiv (IGNORE).")
                        return None, "IRRELEVANT"

                    return generated_text, "SUCCESS"

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                err_name = type(e).__name__
                logger.warning(f"Gemini API {err_name} on attempt {attempt + 1}")
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return None, "ERROR"
            except Exception as e:
                logger.error(f"Unexpected error calling Gemini API: {e}", exc_info=True)
                return None, "ERROR"

        return None, "ERROR"


def create_ai_rewriter(key: str, provider: Optional[str] = None):
    """Auto-detects whether to use Groq or Gemini based on key format or provider setting."""
    if not key or "your_" in str(key):
        return None

    clean_key = str(key).strip().strip("'").strip('"')

    if clean_key.startswith("gsk_") or provider == "groq":
        logger.info("Активовано ШІ Groq (модель qwen/qwen3.8-27b)")
        return GroqRewriter(clean_key)
    else:
        logger.info("Активовано ШІ Google Gemini")
        return GeminiRewriter(clean_key)
