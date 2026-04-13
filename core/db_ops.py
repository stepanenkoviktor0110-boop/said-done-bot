"""Database query functions for said-done-bot."""

from core.db import get_db

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_user(telegram_user_id: int, telegram_username: str | None):
    db = get_db()
    db.execute(
        """INSERT INTO users (telegram_user_id, telegram_username, trial_remaining)
           VALUES (?, ?, 20)
           ON CONFLICT(telegram_user_id) DO UPDATE SET
             telegram_username = excluded.telegram_username,
             last_active_at = CURRENT_TIMESTAMP""",
        (telegram_user_id, telegram_username),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()


def get_user(telegram_user_id: int):
    return get_db().execute(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Voice requests
# ---------------------------------------------------------------------------


def create_voice_request(user_id: int, file_id: str, duration: int | None):
    cur = get_db().execute(
        "INSERT INTO voice_requests (user_id, telegram_file_id, duration_seconds) VALUES (?, ?, ?)",
        (user_id, file_id, duration),
    )
    get_db().commit()
    return get_db().execute(
        "SELECT * FROM voice_requests WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def update_voice_request(vr_id: int, task_count: int | None = None,
                         transcript_length: int | None = None,
                         action_type: str | None = None,
                         summary_length: int | None = None,
                         audio_path: str | None = None,
                         transcript: str | None = None,
                         llm_output: str | None = None):
    sets, vals = [], []
    if task_count is not None:
        sets.append("task_count = ?"); vals.append(task_count)
    if transcript_length is not None:
        sets.append("transcript_length = ?"); vals.append(transcript_length)
    if action_type is not None:
        sets.append("action_type = ?"); vals.append(action_type)
    if summary_length is not None:
        sets.append("summary_length = ?"); vals.append(summary_length)
    if audio_path is not None:
        sets.append("audio_path = ?"); vals.append(audio_path)
    if transcript is not None:
        sets.append("transcript = ?"); vals.append(transcript)
    if llm_output is not None:
        sets.append("llm_output = ?"); vals.append(llm_output)
    if sets:
        vals.append(vr_id)
        get_db().execute(f"UPDATE voice_requests SET {', '.join(sets)} WHERE id = ?", vals)
        get_db().commit()


def get_voice_request(vr_id: int):
    return get_db().execute(
        "SELECT * FROM voice_requests WHERE id = ?", (vr_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Trial
# ---------------------------------------------------------------------------


def decrement_trial(user_id: int) -> int:
    get_db().execute(
        "UPDATE users SET trial_remaining = MAX(trial_remaining - 1, 0) WHERE id = ?",
        (user_id,),
    )
    get_db().commit()
    row = get_db().execute(
        "SELECT trial_remaining FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return row["trial_remaining"]


def complete_survey(user_id: int):
    get_db().execute(
        "UPDATE users SET trial_phase = 2, trial_remaining = 10 WHERE id = ?",
        (user_id,),
    )
    get_db().commit()


def block_survey(user_id: int):
    get_db().execute(
        "UPDATE users SET survey_blocked = 1 WHERE id = ?", (user_id,)
    )
    get_db().commit()


def advance_survey(user_id: int):
    get_db().execute(
        "UPDATE users SET survey_progress = survey_progress + 1 WHERE id = ?",
        (user_id,),
    )
    get_db().commit()


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------


def save_survey_response(user_id: int, question_num: int, answer: str,
                         is_adequate: int, rejection_reason: str | None):
    get_db().execute(
        "INSERT INTO survey_responses (user_id, question_num, answer, is_adequate, rejection_reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, question_num, answer, is_adequate, rejection_reason),
    )
    get_db().commit()


def set_in_survey(user_id: int, value: bool):
    get_db().execute(
        "UPDATE users SET in_survey = ? WHERE id = ?",
        (1 if value else 0, user_id),
    )
    get_db().commit()


def get_user_field(telegram_user_id: int, field: str):
    row = get_db().execute(
        f"SELECT {field} FROM users WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    return row[field] if row else None


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def save_feedback(vr_id: int, rating: int, comment: str | None, voice_consent: int):
    get_db().execute(
        "INSERT INTO feedback (voice_request_id, rating, comment, voice_consent) VALUES (?, ?, ?, ?)",
        (vr_id, rating, comment, voice_consent),
    )
    get_db().commit()
