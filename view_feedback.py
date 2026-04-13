#!/usr/bin/env python3
"""Quick viewer: show voice requests with feedback and LLM output."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.db import init_db, get_db

DB_PATH = Path(__file__).resolve().parent / "data" / "bot.db"
init_db(str(DB_PATH))

db = get_db()

rows = db.execute("""
    SELECT
        vr.id, u.telegram_username, vr.transcript, vr.action_type,
        vr.task_count, vr.llm_output, vr.summary_length,
        f.rating, f.comment, f.voice_consent, vr.created_at
    FROM voice_requests vr
    JOIN users u ON u.id = vr.user_id
    LEFT JOIN feedback f ON f.voice_request_id = vr.id
    ORDER BY vr.created_at DESC
    LIMIT 20
""").fetchall()

for r in rows:
    print(f"\n{'='*60}")
    print(f"ID={r['id']} | @{r['telegram_username']} | {r['action_type']} | {r['created_at']}")
    print(f"Rating: {r['rating']}" + (f" | Comment: {r['comment']}" if r['comment'] else ""))
    print(f"Transcript ({r['transcript_length']} chars): {r['transcript'][:200]}")
    if r['llm_output']:
        out = r['llm_output']
        if len(out) > 300:
            out = out[:300] + "..."
        print(f"LLM output: {out}")
