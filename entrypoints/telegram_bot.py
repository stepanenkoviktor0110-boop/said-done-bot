"""Said-Done Bot — minimal voice → transcript → summary button."""

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

import yaml
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.bot import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
    BotCommandScopeAllPrivateChats,
)

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.transcriber import transcribe_ogg

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

TG_TOKEN = cfg["telegram"]["bot_token"]
OWNER_ID = cfg["owner_id"]
OR_API_KEY = cfg["llm"]["openrouter"]["api_key"]
OR_MODEL = cfg["llm"]["openrouter"]["model"]

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()
_voice_transcripts: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

import httpx

SUMMARY_PROMPT = (
    "Ты делаешь краткое резюме голосового сообщения. "
    "Пиши от первого лица, как если бы говорящий сам сформулировал свои мысли. "
    "Убери словесный мусор, повторы, ложные старты. "
    "Выведи 2-5 ключевых пунктов в виде маркированного списка. "
    "Не выдумывай факты, которых не было в транскрипте."
)


async def llm_summarize(transcript: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OR_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://said-done-bot",
            },
            json={
                "model": OR_MODEL,
                "messages": [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.reply(
        "Привет! Перешли мне голосовое — я расшифрую и сделаю резюме.\n\n"
        "🎙️ Просто отправь голосовое сообщение."
    )


@router.message(F.voice)
async def handle_voice(message: Message):
    wait = await message.reply("🎙️ Распознаю голосовое...")
    try:
        file = await message.bot.get_file(message.voice.file_id)
        ogg_bytes = await message.bot.download_file(file.file_path)
        text = await transcribe_ogg(ogg_bytes.read())
    except Exception as exc:
        await wait.delete()
        logger.error("Transcription failed: %s", exc)
        await message.reply("Не удалось распознать голосовое. Попробуй ещё раз.")
        return

    await wait.delete()

    if not text:
        await message.reply("Не удалось разобрать речь.")
        return

    logger.info("Voice transcribed (user=%s, len=%d)", message.from_user.id, len(text))

    # Show transcript + summary button
    cid = str(uuid.uuid4())[:8]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Сделать резюме", callback_data=f"voice_summary:{cid}"),
    ]])
    _voice_transcripts[cid] = {
        "text": text,
        "user_id": message.from_user.id,
    }
    await message.reply(f"🗣 Распознано: {text}", reply_markup=kb)


@router.callback_query(F.data.startswith("voice_summary:"))
async def handle_voice_summary(callback: CallbackQuery):
    parts = callback.data.split(":")
    cid = parts[1]
    data = _voice_transcripts.pop(cid, None)

    if not data:
        await callback.answer("Запрос устарел, отправь голосовое заново.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()

    await callback.message.reply("🧠 Анализирую голосовое...")
    try:
        summary = await llm_summarize(data["text"])
        await callback.message.reply(f"📝 Резюме:\n\n{summary}")
    except Exception as exc:
        logger.error("Summarization failed: %s", exc)
        await callback.message.reply("Не удалось сделать резюме. Попробуй ещё раз.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    commands = [BotCommand(command="start", description="Начать")]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    logger.info("Starting Said-Done bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
