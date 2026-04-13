"""SQLite database connection and schema."""

import sqlite3
import os

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER UNIQUE NOT NULL,
  telegram_username TEXT,
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_active_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  total_voice_count INTEGER NOT NULL DEFAULT 0,
  trial_remaining INTEGER NOT NULL DEFAULT 20,
  trial_phase INTEGER NOT NULL DEFAULT 1,
  survey_progress INTEGER NOT NULL DEFAULT 0,
  survey_blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS voice_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  telegram_file_id TEXT NOT NULL,
  duration_seconds INTEGER,
  task_count INTEGER,
  transcript_length INTEGER,
  action_type TEXT,
  summary_length INTEGER,
  audio_path TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  voice_request_id INTEGER NOT NULL REFERENCES voice_requests(id),
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment TEXT,
  voice_consent INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS survey_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  question_num INTEGER NOT NULL,
  answer TEXT NOT NULL,
  is_adequate INTEGER NOT NULL DEFAULT 1,
  rejection_reason TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_db = None


def init_db(db_path: str):
    """Initialize SQLite connection, create schema."""
    global _db
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    _db = sqlite3.connect(db_path, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode = WAL")
    _db.execute("PRAGMA foreign_keys = ON")
    _db.executescript(SCHEMA_SQL)


def get_db():
    """Return the active database connection."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db
