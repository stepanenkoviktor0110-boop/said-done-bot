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
from core import task_extractor as task_extractor_mod
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
_bot_instance: Bot | None = None  # stored for expire timer

# Multi-voice debounce: chat_id → {voices: [{file_id, duration, msg}], timer, reply_ctx}
_voice_buffers: dict[int, dict] = {}
MAX_BATCH = 5
DEBOUNCE_SECS = 3

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
    sess["action_msg_id"] = None
    sess["action_chat_id"] = None


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
# Voice handler — debounce buffer for multi-voice
# ---------------------------------------------------------------------------


async def _process_voice_buffer(chat_id: int, uid: int, bot):
    """Transcribe all buffered voices, show combined result with progress."""
    buf = _voice_buffers.pop(chat_id, None)
    if not buf:
        return

    voices = buf["voices"][:MAX_BATCH]
    total = len(buf["voices"])
    exceeded = total > MAX_BATCH

    # Show processing status
    if total == 1:
        wait = await bot.send_message(chat_id=chat_id, text=messages.PROCESSING)
    else:
        wait = await bot.send_message(chat_id=chat_id, text=f"🔄 Обрабатываю {total} голосовых...")

    transcripts = []
    failed = 0

    for i, v in enumerate(voices, 1):
        # Show progress by COUNT — honest and predictable
        if total > 1:
            pct_before = int((i - 1) / total * 100)
            bar = "█" * (pct_before // 5) + "░" * (20 - pct_before // 5)
            try:
                await wait.edit_text(f"🎙 Транскрибация: {i}/{total} ({pct_before}%)\n[{bar}]")
            except Exception:
                pass
        try:
            file = await bot.get_file(v["file_id"])
            ogg_bytes = await bot.download_file(file.file_path)
            text = await transcribe_ogg(ogg_bytes.read())
            if text:
                transcripts.append(text)
            else:
                failed += 1
        except Exception as exc:
            logger.error("Voice transcription failed: %s", exc)
            failed += 1

    # Final: 100%
    if total > 1:
        bar = "█" * 20
        try:
            await wait.edit_text(f"🎙 Готово! {total}/{total} (100%)\n[{bar}]")
        except Exception:
            pass

    await wait.delete()

    if not transcripts:
        await bot.send_message(chat_id=chat_id, text=messages.ALL_FAILED)
        return

    # Merge transcripts via LLM when multiple voices
    if len(transcripts) > 1:
        try:
            combined = await task_extractor_mod.merge_transcripts(transcripts, OR_API_KEY, OR_MODEL)
        except Exception as exc:
            logger.error("LLM merge failed: %s", exc)
            combined = " ".join(transcripts)  # fallback: simple join
    else:
        combined = transcripts[0]

    # Build UI notes
    ui_notes = ""
    if exceeded:
        ui_notes += messages.BATCH_LIMIT
    if failed:
        ui_notes += messages.failure_note(failed, total)
    if any(v.get("is_long") for v in voices):
        ui_notes += "\n\n" + messages.LONG_VOICE
    if len(combined) > 4000:
        ui_notes += messages.TRUNCATED_NOTE
        combined = combined[:4000]
    if total > 1:
        ui_notes += "\n\n" + messages.MERGED_NOTE

    logger.info("Multi-voice batch: chatId=%s, total=%d, processed=%d, failed=%d",
                chat_id, total, len(transcripts), failed)

    await _show_transcript_and_actions(chat_id, uid, combined, bot, ui_notes=ui_notes,
                                       voice_count=total, file_id=voices[0]["file_id"],
                                       duration=sum(v.get("duration", 0) for v in voices))


@router.message(F.voice)
async def handle_voice(msg: Message):
    chat_id = msg.chat.id
    uid = msg.from_user.id

    # If we're in feedback/survey flow, new voice abandons it
    sess = _get_session(uid)
    if sess.get("awaiting_feedback") or sess.get("awaiting_comment") or sess.get("awaiting_consent"):
        _clear_feedback_state(sess)
    sess["awaiting_action"] = False
    sess["action_done"] = None
    sess["action_msg_id"] = None
    sess["action_chat_id"] = None

    user = db_ops.upsert_user(uid, msg.from_user.username)

    # Trial check
    if user["trial_remaining"] == 0:
        if user["trial_phase"] == 1 and not user["survey_blocked"] and user["survey_progress"] < 4:
            await _enter_survey(msg, user)
            return
        await msg.reply(messages.TRIAL_EXHAUSTED)
        return

    # Add to debounce buffer
    if chat_id not in _voice_buffers:
        _voice_buffers[chat_id] = {"voices": [], "timer": None}

    buf = _voice_buffers[chat_id]
    is_long = msg.voice.duration and msg.voice.duration > 180
    buf["voices"].append({"file_id": msg.voice.file_id, "duration": msg.voice.duration, "msg": msg, "is_long": is_long})

    # Warn if batch already large
    if len(buf["voices"]) >= MAX_BATCH:
        # Process immediately — already at limit
        if buf["timer"]:
            buf["timer"].cancel()
        await _process_voice_buffer(chat_id, uid, msg.bot)
        return

    # Reset timer on each new voice
    if buf["timer"]:
        buf["timer"].cancel()

    loop = asyncio.get_event_loop()
    buf["timer"] = loop.call_later(
        DEBOUNCE_SECS,
        lambda: asyncio.create_task(_process_voice_buffer(chat_id, uid, msg.bot))
    )

    # Show queue status
    limit = MAX_BATCH - len(buf["voices"])
    if len(buf["voices"]) == 1:
        await msg.reply(f"🎙 Принято первое голосовое. Жду ещё {DEBOUNCE_SECS} сек (можно ещё {limit})...")
    else:
        await msg.reply(f"🎙 Принято {len(buf['voices'])} из {MAX_BATCH}. Жду ещё... (осталось {limit})")


async def _show_transcript_and_actions(chat_id: int, uid: int, transcript: str, bot,
                                       ui_notes: str = "", voice_count: int = 1,
                                       file_id: str = "", duration: int = 0):
    """Show transcript + action buttons for a single or batch of voices."""
    user = db_ops.upsert_user(uid, None)
    vr = db_ops.create_voice_request(user["id"], file_id, duration)
    db_ops.update_voice_request(vr["id"], transcript=transcript, transcript_length=len(transcript))

    sess = _get_session(uid)
    cid = str(uuid.uuid4())[:8]
    _voice_transcripts[cid] = {"text": transcript, "user_id": uid, "vr_id": vr["id"]}

    sess["awaiting_action"] = True
    sess["transcript_cid"] = cid
    sess["action_started_at"] = asyncio.get_event_loop().time()
    sess["action_done"] = None

    header = f"🗣 Принято {voice_count} голосовых" if voice_count > 1 else "🗣 Распознано"
    notes_prefix = "\n\n" if ui_notes else ""
    text = f"{header}: {transcript}\n\n{messages.ACTION_KEYBOARD}{ui_notes}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=messages.ACTION_TASKS, callback_data="action:tasks"),
        InlineKeyboardButton(text=messages.ACTION_SUMMARY, callback_data="action:summary"),
    ]])
    reply = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    sess["action_msg_id"] = reply.message_id
    sess["action_chat_id"] = chat_id


# ---------------------------------------------------------------------------
# Action: tasks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:tasks")
async def action_tasks(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)
    cid = sess.get("transcript_cid")
    data = _voice_transcripts.get(cid)  # Don't pop — keep for 60s so the other button works

    if not data:
        await cb.answer(messages.ACTION_EXPIRED, show_alert=True)
        return

    await cb.answer()

    # Check if the other action was already clicked — if so, expire after this
    was_both_clicked = sess.get("action_done") is not None

    sess["action_done"] = "tasks"
    sess["action_started_at"] = asyncio.get_event_loop().time()
    sess["awaiting_action"] = False

    user = db_ops.upsert_user(uid, cb.from_user.username)
    if user["trial_remaining"] == 0:
        await cb.message.reply(messages.TRIAL_EXHAUSTED)
        return

    status_msg = await cb.message.reply("🧠 Извлекаю задачи...")
    try:
        result = await task_extractor_mod.extract_tasks(data["text"], OR_API_KEY, OR_MODEL)
    except Exception as exc:
        await status_msg.delete()
        logger.error("Task extraction failed: %s", exc)
        await cb.message.reply(messages.GENERIC_ERROR)
        return
    await status_msg.delete()

    vr_id = data["vr_id"]
    # Save LLM output for debugging — full result as JSON
    import json
    llm_out = json.dumps(result, ensure_ascii=False, default=str)
    db_ops.update_voice_request(vr_id, action_type="tasks",
                                task_count=len(result.get("tasks", [])),
                                llm_output=llm_out)
    db_ops.decrement_trial(user["id"])

    # Build rating keyboard — always shown regardless of result
    rate_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1", callback_data="rate:1"),
        InlineKeyboardButton(text="2", callback_data="rate:2"),
        InlineKeyboardButton(text="3", callback_data="rate:3"),
        InlineKeyboardButton(text="4", callback_data="rate:4"),
        InlineKeyboardButton(text="5", callback_data="rate:5"),
    ]])

    if result.get("marker") == "no_tasks":
        await cb.message.reply(messages.NO_TASKS, reply_markup=rate_kb)
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id
    elif result.get("marker") == "too_many_tasks":
        await cb.message.reply(messages.TOO_MANY, reply_markup=rate_kb)
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id
    elif result.get("marker") == "summary":
        # User asked for tasks but LLM found only summary — show it with note
        summary_text = result.get('summary', '')
        await cb.message.reply(
            f"📝 В этом голосовом нет конкретных задач, но вот ключевые мысли:\n\n{summary_text}",
            reply_markup=rate_kb
        )
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id
    elif result.get("tasks"):
        task_text = messages.format_tasks(result["tasks"])
        if result.get("truncated"):
            task_text += messages.TRUNCATED_NOTE
        await cb.message.reply(task_text, reply_markup=rate_kb)
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id
    else:
        logger.warning("extract_tasks returned unknown format: %s", result.get('error', 'unknown'))
        await cb.message.reply(messages.NO_TASKS, reply_markup=rate_kb)
        sess["awaiting_feedback"] = True
        sess["voice_request_id"] = vr_id

    # Keep action buttons available for 60s so user can try the other option
    # If both were clicked — expire now instead
    if was_both_clicked:
        await _remove_action_buttons(sess)
        asyncio.create_task(_expire_action(cid, sess))
    else:
        asyncio.create_task(_expire_action(cid, sess))

    logger.info("action=tasks: userId=%s, taskCount=%d, marker=%s, raw=%r", uid, len(result.get("tasks", [])), result.get("marker"), result.get("tasks", [])[:2] if result.get("tasks") else "none")


async def _remove_action_buttons(sess: dict):
    """Remove action buttons from the transcript message."""
    msg_id = sess.pop("action_msg_id", None)
    chat_id = sess.pop("action_chat_id", None)
    if msg_id and chat_id and _bot_instance:
        try:
            from aiogram.exceptions import TelegramBadRequest
            await _bot_instance.edit_message_reply_markup(
                chat_id=chat_id, message_id=msg_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass  # already edited or deleted


async def _expire_action(cid: str, sess: dict):
    """Remove action buttons after 60 seconds by editing the message."""
    await asyncio.sleep(60)
    try:
        await _remove_action_buttons(sess)
    except Exception as exc:
        logger.error("_expire_action error: %s", exc)
    finally:
        _voice_transcripts.pop(cid, None)
        sess["action_done"] = None
        sess["transcript_cid"] = None
        sess["awaiting_action"] = False


# ---------------------------------------------------------------------------
# Action: summary
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "action:summary")
async def action_summary(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)
    cid = sess.get("transcript_cid")
    data = _voice_transcripts.get(cid)  # Don't pop — keep for 60s

    if not data:
        await cb.answer(messages.ACTION_EXPIRED, show_alert=True)
        return

    await cb.answer()

    # Check if the other action was already clicked
    was_both_clicked = sess.get("action_done") is not None

    sess["action_done"] = "summary"
    sess["action_started_at"] = asyncio.get_event_loop().time()
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

    db_ops.update_voice_request(data["vr_id"], action_type="summary",
                                summary_length=len(summary),
                                llm_output=summary)
    db_ops.decrement_trial(user["id"])

    rate_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1", callback_data="rate:1"),
        InlineKeyboardButton(text="2", callback_data="rate:2"),
        InlineKeyboardButton(text="3", callback_data="rate:3"),
        InlineKeyboardButton(text="4", callback_data="rate:4"),
        InlineKeyboardButton(text="5", callback_data="rate:5"),
    ]])
    await cb.message.reply(f"📝 Резюме:\n\n{summary}\n\n{messages.SUMMARY_DONE}", reply_markup=rate_kb)
    sess["awaiting_feedback"] = True
    sess["voice_request_id"] = data["vr_id"]
    logger.info("action=summary: userId=%s, len=%d", uid, len(summary))

    # Keep action buttons available for 60s
    # If both were clicked — expire now instead
    if was_both_clicked:
        await _remove_action_buttons(sess)
        asyncio.create_task(_expire_action(cid, sess))
    else:
        asyncio.create_task(_expire_action(cid, sess))


# ---------------------------------------------------------------------------
# Feedback: rating
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("rate:"))
async def handle_rating(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = _get_session(uid)

    if not sess.get("awaiting_feedback"):
        logger.warning("Rating expired for uid=%s, session_keys=%s", uid, list(sess.keys()))
        await cb.answer(messages.FB_EXPIRED)
        return

    rating = int(cb.data.split(":")[1])
    await cb.answer()

    # Remove rating buttons immediately
    await cb.message.edit_reply_markup(reply_markup=None)

    # Also remove action buttons from the transcript message
    await _remove_action_buttons(sess)

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

    _remove_action_buttons(sess)
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
    global _bot_instance
    _bot_instance = bot
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
