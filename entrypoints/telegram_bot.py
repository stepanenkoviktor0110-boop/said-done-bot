"""Said-Done Bot — voice → choose: tasks or summary."""

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
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeAllPrivateChats,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.transcriber import transcribe_ogg
from core.task_extractor import extract_tasks
from core import db as bot_db
from core import db_ops
from core import messages

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
DB_PATH = PROJECT_ROOT / cfg.get("db", {}).get("path", "data/bot.db")

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
_user_state: dict[int, dict] = {}  # user_id → session state

# Summary LLM (kept separate from task extraction)
SUMMARY_PROMPT = (
    "Ты делаешь краткое резюме голосового сообщения. "
    "Пиши от первого лица, как если бы говорящий сам сформулировал свои мысли. "
    "Убери словесный мусор, повторы, ложные старты. "
    "Выведи 2-5 ключевых пунктов в виде маркированного списка. "
    "Не выдумывай факты, которых не было в транскрипте."
)


async def _llm_summarize(transcript: str) -> str:
    import httpx
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


def _get_session(uid: int) -> dict:
    if uid not in _user_state:
        _user_state[uid] = {
            "awaiting_action": False,
            "awaiting_feedback": False,
            "voice_request_id": None,
            "awaiting_comment": False,
            "awaiting_consent": False,
            "pending_rating": None,
            "pending_comment": None,
            "in_survey": False,
            "survey_retries": {},
        }
    return _user_state[uid]


def _clear_feedback_state(sess: dict):
    sess["awaiting_feedback"] = False
    sess["voice_request_id"] = None
    sess["awaiting_comment"] = False
    sess["awaiting_consent"] = False
    sess["pending_rating"] = None
    sess["pending_comment"] = None


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(msg: Message):
    user = db_ops.upsert_user(msg.from_user.id, msg.from_user.username)
    remaining = user["trial_remaining"]
    await msg.reply(messages.WELCOME.format(TRIAL_REMAINING=remaining))


# ---------------------------------------------------------------------------
# Non-voice catch-all
# ---------------------------------------------------------------------------

@router.message(F.message)
async def non_voice_handler(msg: Message, next_handler):
    if msg.voice:
        return await next_handler()
    if msg.text and msg.text.startswith("/"):
        return await next_handler()
    sess = _get_session(msg.from_user.id)
    if sess.get("awaiting_comment") or sess.get("in_survey"):
        return await next_handler()
    await msg.reply(messages.NON_VOICE)


# ---------------------------------------------------------------------------
# Voice handler
# ---------------------------------------------------------------------------

@router.message(F.voice)
async def handle_voice(msg: Message):
    uid = msg.from_user.id
    sess = _get_session(uid)

    # Decision 8: abandon pending feedback on new voice
    if sess.get("awaiting_feedback") or sess.get("awaiting_comment") or sess.get("awaiting_consent"):
        _clear_feedback_state(sess)
    sess["awaiting_action"] = False

    user = db_ops.upsert_user(uid, msg.from_user.username)

    # Trial check
    if user["trial_remaining"] == 0:
        if user["trial_phase"] == 1 and not user["survey_blocked"] and user["survey_progress"] < 4:
            await _enter_survey(msg, user)
            return
        await msg.reply(messages.TRIAL_EXHAUSTED)
        return

    # Transcribe
    wait = await msg.reply(messages.PROCESSING)
    try:
        file = await msg.bot.get_file(msg.voice.file_id)
        ogg_bytes = await msg.bot.download_file(file.file_path)
        text = await transcribe_ogg(ogg_bytes.read())
    except Exception as exc:
        await wait.delete()
        logger.error("Transcription failed: %s", exc)
        await msg.reply(messages.ALL_FAILED)
        return
    await wait.delete()

    if not text:
        await msg.reply("Не удалось разобрать речь.")
        return

    logger.info("Voice transcribed (user=%s, len=%d)", uid, len(text))

    # Create voice request
    vr = db_ops.create_voice_request(user["id"], msg.voice.file_id, msg.voice.duration)

    # Store for callback
    cid = str(uuid.uuid4())[:8]
    _voice_transcripts[cid] = {"text": text, "user_id": uid, "vr_id": vr["id"]}

    sess["awaiting_action"] = True
    sess["transcript_cid"] = cid

    # Show transcript + 2 buttons
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=messages.ACTION_TASKS, callback_data="action:tasks"),
        InlineKeyboardButton(text=messages.ACTION_SUMMARY, callback_data="action:summary"),
    ]])
    await msg.reply(f"🗣 Распознано: {text}\n\n{messages.ACTION_KEYBOARD}", reply_markup=kb)


# ---------------------------------------------------------------------------
# Action: tasks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:tasks")
async def action_tasks(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)
    cid = sess.get("transcript_cid")
    data = _voice_transcripts.pop(cid, None) if cid else None

    if not data:
        await cb.answer(messages.ACTION_EXPIRED, show_alert=True)
        return

    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    sess["awaiting_action"] = False

    user = db_ops.upsert_user(uid, cb.from_user.username)
    if user["trial_remaining"] == 0:
        await cb.message.reply(messages.TRIAL_EXHAUSTED)
        return

    status_msg = await cb.message.reply("🧠 Извлекаю задачи...")
    try:
        result = await extract_tasks([data["text"]], OR_API_KEY, OR_MODEL)
    except Exception as exc:
        await status_msg.delete()
        logger.error("Task extraction failed: %s", exc)
        await cb.message.reply(messages.GENERIC_ERROR)
        return
    await status_msg.delete()

    vr_id = data["vr_id"]
    db_ops.update_voice_request(vr_id, action_type="tasks")
    db_ops.decrement_trial(user["id"])

    if result.get("marker") == "no_tasks":
        await cb.message.reply(messages.NO_TASKS)
    elif result.get("marker") == "too_many_tasks":
        await cb.message.reply(messages.TOO_MANY)
    elif result.get("marker") == "summary":
        await cb.message.reply(f"📝 {result.get('summary', '')}")
    elif result.get("tasks"):
        task_text = messages.format_tasks(result["tasks"])
        if result.get("truncated"):
            task_text += messages.TRUNCATED_NOTE
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="1", callback_data="rate:1"),
            InlineKeyboardButton(text="2", callback_data="rate:2"),
            InlineKeyboardButton(text="3", callback_data="rate:3"),
            InlineKeyboardButton(text="4", callback_data="rate:4"),
            InlineKeyboardButton(text="5", callback_data="rate:5"),
        ]])
        await cb.message.reply(task_text, reply_markup=kb)
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id
    else:
        await cb.message.reply(messages.NO_TASKS)

    logger.info("action=tasks: userId=%s, taskCount=%d", uid, len(result.get("tasks", [])))


# ---------------------------------------------------------------------------
# Action: summary
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:summary")
async def action_summary(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)
    cid = sess.get("transcript_cid")
    data = _voice_transcripts.pop(cid, None) if cid else None

    if not data:
        await cb.answer(messages.ACTION_EXPIRED, show_alert=True)
        return

    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    sess["awaiting_action"] = False

    user = db_ops.upsert_user(uid, cb.from_user.username)
    if user["trial_remaining"] == 0:
        await cb.message.reply(messages.TRIAL_EXHAUSTED)
        return

    await cb.message.reply("🧠 Анализирую голосовое...")
    try:
        summary = await _llm_summarize(data["text"])
    except Exception as exc:
        logger.error("Summarization failed: %s", exc)
        await cb.message.reply(messages.GENERIC_ERROR)
        return

    db_ops.update_voice_request(data["vr_id"], action_type="summary", summary_length=len(summary))
    db_ops.decrement_trial(user["id"])

    await cb.message.reply(f"📝 Резюме:\n\n{summary}\n\n{messages.SUMMARY_DONE}")
    logger.info("action=summary: userId=%s, len=%d", uid, len(summary))


# ---------------------------------------------------------------------------
# Feedback: rating
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("rate:"))
async def handle_rating(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)

    if not sess.get("awaiting_feedback"):
        await cb.answer(messages.FB_EXPIRED)
        return

    rating = int(cb.data.split(":")[1])
    await cb.answer()

    if rating == 5:
        db_ops.save_feedback(sess["voice_request_id"], 5, None, 0)
        _clear_feedback_state(sess)
        await cb.message.reply(messages.FB_THANKS)
        return

    sess["pending_rating"] = rating
    sess["awaiting_comment"] = True
    await cb.message.reply(messages.FB_COMMENT)


# ---------------------------------------------------------------------------
# Feedback: comment → consent
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_text(msg: Message):
    uid = msg.from_user.id
    sess = _get_session(uid)

    # Survey answer
    if sess.get("in_survey"):
        user = db_ops.get_user(uid)
        if user:
            await _handle_survey_answer(msg, user, sess)
        return

    # Feedback comment
    if sess.get("awaiting_comment"):
        sess["pending_comment"] = msg.text
        sess["awaiting_comment"] = False
        sess["awaiting_consent"] = True
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да", callback_data="consent:yes"),
            InlineKeyboardButton(text="Нет", callback_data="consent:no"),
        ]])
        await msg.reply(messages.FB_CONSENT, reply_markup=kb)
        return

    if sess.get("awaiting_consent"):
        await msg.reply(messages.FB_BUTTONS)
        return


@router.callback_query(F.data.startswith("consent:"))
async def handle_consent(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)

    if not sess.get("awaiting_consent"):
        await cb.answer(messages.FB_EXPIRED)
        return

    consent = 1 if cb.data == "consent:yes" else 0
    await cb.answer()

    vr_id = sess.get("voice_request_id")
    rating = sess.get("pending_rating")
    comment = sess.get("pending_comment")

    db_ops.save_feedback(vr_id, rating, comment, consent)

    if consent == 1 and vr_id:
        try:
            vr = db_ops.get_voice_request(vr_id)
            if vr and vr["telegram_file_id"]:
                fi = await cb.message.bot.get_file(vr["telegram_file_id"])
                audio = await cb.message.bot.download_file(fi.file_path)
                voices_dir = PROJECT_ROOT / "data" / "voices"
                voices_dir.mkdir(parents=True, exist_ok=True)
                rel = f"data/voices/{vr_id}.oga"
                (PROJECT_ROOT / rel).write_bytes(audio)
                db_ops.update_voice_request(vr_id, audio_path=rel)
        except Exception as exc:
            logger.error("Failed to save voice audio: %s", exc)

    _clear_feedback_state(sess)
    await cb.message.reply(messages.FB_SAVED)


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------

SANITY_PROMPT = (
    "Ты проверяешь ответы на опрос о Telegram-боте. "
    "Определи: ответ осмысленный и по теме, или это мусор. "
    "Ответь одним словом: 'адекватный' или 'неадекватный'."
)


def _passes_heuristic(q_idx: int, answer: str) -> bool:
    t = answer.strip()
    if not t:
        return False
    if q_idx == 3:
        return bool(__import__("re").match(r"^[1-5]$", t))
    if all(c in " \t\n\r!?.,;:—–…@#$%^&*()_+=-[]{}|\\<>/~`" for c in t):
        return False
    return len(t.split()) >= 5


async def _enter_survey(msg, user):
    sess = _get_session(msg.from_user.id)
    sess["in_survey"] = True
    db_ops.set_in_survey(user["id"], True)
    q_idx = user["survey_progress"]
    intro = messages.SURVEY_INTRO + "\n\n" if q_idx == 0 else ""
    await msg.reply(intro + messages.survey_q(q_idx))


async def _handle_survey_answer(msg: Message, user, sess: dict):
    q_idx = user["survey_progress"]
    if q_idx >= 4:
        return

    answer = msg.text
    question_text = messages.SURVEY_QUESTIONS[q_idx]

    is_adequate = _passes_heuristic(q_idx, answer)
    reason = None if is_adequate else "heuristic"

    if is_adequate and q_idx < 3:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OR_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": OR_MODEL,
                        "messages": [
                            {"role": "system", "content": SANITY_PROMPT},
                            {"role": "user", "content": f"Вопрос: {question_text}\nОтвет: {answer}"},
                        ],
                        "max_tokens": 10,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    llm_text = data["choices"][0]["message"]["content"].lower()
                    if "неадекватный" in llm_text:
                        is_adequate = False
                        reason = "llm"
        except Exception as exc:
            logger.error("LLM sanity check failed: %s", exc)
            is_adequate = True  # fail-open

    if not is_adequate:
        retry_key = str(q_idx)
        retries = sess["survey_retries"].get(retry_key, 0)
        db_ops.save_survey_response(user["id"], q_idx + 1, answer, 0, reason)

        if retries >= 1:
            db_ops.block_survey(user["id"])
            sess["in_survey"] = False
            sess["survey_retries"] = {}
            logger.info("survey_blocked: userId=%s, q=%d", user["id"], q_idx + 1)
            await msg.reply(messages.SURVEY_BLOCKED)
            return

        sess["survey_retries"][retry_key] = retries + 1
        await msg.reply(messages.SURVEY_RETRY)
        return

    # Adequate
    db_ops.save_survey_response(user["id"], q_idx + 1, answer, 1, None)
    db_ops.advance_survey(user["id"])
    next_idx = q_idx + 1

    if next_idx >= 4:
        db_ops.complete_survey(user["id"])
        sess["in_survey"] = False
        sess["survey_retries"] = {}
        await msg.reply(messages.SURVEY_COMPLETE)
        return

    await msg.reply(messages.survey_q(next_idx))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    bot_db_path = str(DB_PATH)
    os.makedirs(os.path.dirname(bot_db_path) if os.path.dirname(bot_db_path) else ".", exist_ok=True)
    bot_db.init_db(bot_db_path)

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
