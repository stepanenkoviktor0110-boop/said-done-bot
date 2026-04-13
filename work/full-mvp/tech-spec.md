---
created: 2026-04-14
status: draft
size: XL
---

# Tech Spec: said-done-bot — Full MVP (voice → choose → tasks or summary)

## Context

Current `said-done-bot` implements only: voice → transcribe → show transcript + [📝 Сделать резюме] → summary.

Missing from original ToTalk-ToDo spec: task extraction, action:tasks button, rating/feedback flow, trial system with survey, multi-voice debounce buffer, SQLite database.

We extend the existing bot by adding all missing features while keeping the same stack: Python/aiogram, faster-whisper in-process, OpenRouter LLM.

## Architecture

### What we're building/modifying

| File | Current | After |
|------|---------|-------|
| `entrypoints/telegram_bot.py` | voice handler + summary button | voice handler + 2 action buttons, task extraction callback, rating/feedback handler, survey handler |
| `core/transcriber.py` | ✅ works | unchanged |
| `core/db.py` | — | new: SQLite init, schema, connection |
| `core/db_ops.py` | — | new: user CRUD, voice requests, feedback, survey, trial ops |
| `core/task_extractor.py` | — | new: reads prompt file, calls LLM, parses numbered list |
| `prompts/task-extraction.md` | — | new: system prompt for task extraction |
| `core/messages.py` | — | new: all user-facing Russian strings |

### Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Runtime | Python 3.12 | Same as МаркетБот, shared venv |
| Telegram | aiogram 3.x | Same as МаркетБот |
| STT | faster-whisper in-process | No HTTP server needed, same as МаркетБот |
| LLM | OpenRouter (qwen/qwen-2.5-72b-instruct) | Same API key as МаркетБот |
| DB | better-sqlite3 (sync) | 5 testers, no infra needed |
| Hosting | systemd on same VPS | Same pattern as marketbot.service |

### Data Flow

```
voice → download → transcribe → show transcript + [📋 tasks] [📝 summary]
                                                          ↓
                              ┌───────────────────────────┴──────────────────────────┐
                              ↓                                                      ↓
                    click 📋: extract tasks → numbered list → rating 1-5 → feedback    click 📝: LLM summary → done
```

### Session State (in-memory dict, per-user)

```python
_user_state: dict[int, dict] = {
    "awaiting_action": False,    # waiting for task/summary button
    "voice_request_id": None,    # which VR to associate feedback with
    "awaiting_comment": False,   # waiting for text comment after rating <5
    "awaiting_consent": False,   # waiting for voice consent Yes/No
    "in_survey": False,          # user is in trial survey flow
    "survey_retries": {},        # {question_num: retry_count}
    "pending_rating": None,      # rating value (1-5)
    "pending_comment": None,     # comment text
}
```

## Decisions

### Decision 1: Two action buttons after transcription
**Decision:** After transcription, show transcript with inline keyboard [📋 Извлечь задачи] [📝 Сделать резюме]. User picks one.
**Rationale:** User wants choice — not all voice messages contain tasks. Some are status updates, observations, agreements.
**Alternatives considered:** Auto task extraction (original ToTalk-ToDo) — wastes tokens on non-task voices; Summary only (current) — no task extraction at all.

### Decision 2: Summary has no rating flow
**Decision:** After summary is shown, no 1-5 rating. Just the summary text.
**Rationale:** Summary is informational, not a deliverable. Rating is for task extraction quality.
**Alternatives considered:** Rating on both paths (more data but more friction).

### Decision 3: Task extraction prompt as separate file
**Decision:** System prompt stored in `prompts/task-extraction.md`, not hardcoded.
**Rationale:** Prompt iteration doesn't require code changes. Same pattern as МаркетБот.

### Decision 4: Multi-voice debounce buffer
**Decision:** On first voice in a chat, start 3-second timer. Collect all voices. Process as batch after timer fires.
**Rationale:** Telegram delivers forwarded messages as separate events.
**Alternatives considered:** Process each independently (defeats multi-voice requirement).

### Decision 5: Transcript truncation at 4000 chars
**Decision:** Truncate combined transcript to 4000 chars before LLM call. Notify user.
**Rationale:** OpenRouter token limits. 4000 chars ≈ 5-7 min of speech.

### Decision 6: Survey sanity check with heuristic gate + LLM
**Decision:** Before LLM sanity check, apply heuristic gate: Q1-Q3 reject under 5 words or punctuation-only; Q4 accept single digit 1-5. If OpenRouter unavailable → accept (fail-open).
**Rationale:** Blocks trivial bypass even during LLM downtime. Q4 exemption prevents rejecting valid numeric answers.

### Decision 7: Trial numbers: 20 + 10
**Decision:** New users get 20. Survey completion gives +10. Total 30. After that — full block.
**Rationale:** User decision.

### Decision 8: Feedback interruption — silent abandon
**Decision:** If new voice arrives while rating/comment/consent pending, silently abandon previous feedback and process new voice.
**Rationale:** Users should not be blocked. Forwarded voices are primary use case.

### Decision 9: No per-user rate limiting in MVP
**Decision:** No cooldown. Trial counter (30 total) is the natural abuse cap.
**Rationale:** 5 testers × 30 = 150 requests total. OpenRouter free tier is sufficient.

### Decision 10: Shared venv with МаркетБот
**Decision:** Use `/home/xander_bot/botz/МаркетБот/.venv` instead of creating a separate venv.
**Rationale:** Same dependencies (aiogram, faster-whisper, httpx, pyyaml). Saves disk space and maintenance.

## Data Models

### SQLite Schema

```sql
CREATE TABLE users (
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

CREATE TABLE voice_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  telegram_file_id TEXT NOT NULL,
  duration_seconds INTEGER,
  task_count INTEGER,
  transcript_length INTEGER,
  action_type TEXT,       -- 'tasks' | 'summary'
  summary_length INTEGER,
  audio_path TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  voice_request_id INTEGER NOT NULL REFERENCES voice_requests(id),
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment TEXT,
  voice_consent INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE survey_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  question_num INTEGER NOT NULL,
  answer TEXT NOT NULL,
  is_adequate INTEGER NOT NULL DEFAULT 1,
  rejection_reason TEXT,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### LLM Prompts

**Task extraction** (`prompts/task-extraction.md`):
- Input: transcript(s)
- Output: numbered task list, or `__NO_TASKS__`, `__TOO_MANY_TASKS__`, `__SUMMARY__`
- Rules: remove verbal noise, resolve ambiguities, order by dependencies

**Summary** (inline in code):
- Input: transcript
- Output: first-person summary, 2-5 bullet points

## Testing Strategy

**Unit tests:**
- task_extractor: parse numbered list, no_tasks, too_many_tasks, summary markers
- db_ops: upsertUser, createVoiceRequest, saveFeedback, trial ops, survey ops
- debounce buffer: collect 3 voices in 3s, fire once, voice after = new buffer
- survey: heuristic gate (Q1-Q3 ≥5 words, Q4 digit 1-5), 2-strike rule, fail-open

**Integration tests:**
- Full pipeline: voice → transcribe → click tasks → tasks + rating → rate 5 → feedback saved
- Full pipeline: voice → transcribe → click summary → summary → no rating
- Trial lifecycle: 20 decrements → survey trigger → 4 adequate answers → +10 → 10 decrements → block
- Multi-voice: 3 voices → 1 combined result

**E2E (manual, 5 scenarios):**
1. Single voice → tasks → rate 5 (happy path)
2. Single voice → summary → done
3. Three voices → combined task list
4. Trial exhausted → survey → unlock
5. Text message → explanation

## Risks

| Risk | Mitigation |
|------|-----------|
| LLM quality for Russian conversational extraction | Provider swappable via config |
| faster-whisper on CPU slow | model: small, int8, ~2× realtime |
| better-sqlite3 blocks event loop | Acceptable for 5 testers; async in v2 |
| Long transcript exceeds token limit | Truncate to 4000 chars with notification |
| Survey exploit with gibberish | Heuristic gate + LLM sanity check + 2-strike rule |

## Acceptance Criteria

- [ ] All unit tests pass
- [ ] Integration tests pass (pipeline, feedback, trial, multi-voice)
- [ ] DB migrations run on fresh database
- [ ] Bot starts, responds to /start with welcome + "20 бесплатных"
- [ ] Voice → transcript + [📋 Извлечь задачи] [📝 Сделать резюме]
- [ ] 📋 → task list with rating 1-5
- [ ] 📝 → summary, no rating
- [ ] Rating < 5 → comment → consent → save
- [ ] Trial: 20 → survey → +10 → block
- [ ] Multi-voice debounce: 3s window, combined context
- [ ] Transcript truncation at 4000 chars with notification
- [ ] No credentials, transcripts, or PII in logs
- [ ] Bot doesn't crash on any message type
- [ ] Response time < 30s for voice up to 1 min

## Implementation Tasks

### Wave 1 (independent — Infrastructure)

#### Task 1: Database layer — schema, migration, query functions
- **Files to create:** `core/db.py`, `core/db_ops.py`, `core/db_schema.sql`
- **Files to modify:** `entrypoints/telegram_bot.py` (import db_ops)

#### Task 2: Messages module — centralized user-facing strings
- **Files to create:** `core/messages.py`
- **Files to modify:** `entrypoints/telegram_bot.py` (import messages)

#### Task 3: Task extraction service — prompt file + parser + LLM wrapper
- **Files to create:** `core/task_extractor.py`, `prompts/task-extraction.md`
- **Files to modify:** none (new module)

#### Task 4: Update voice handler — two action buttons instead of one
- **Files to modify:** `entrypoints/telegram_bot.py` (handle_voice)

### Wave 2 (depends on Wave 1 — Core features)

#### Task 5: Callback handler — 📋 Извлечь задачи
- **Files to modify:** `entrypoints/telegram_bot.py` (new callback handler)

#### Task 6: Multi-voice debounce buffer
- **Files to modify:** `entrypoints/telegram_bot.py` (handle_voice + process_voice_batch)

#### Task 7: Rating + feedback flow (comment → consent → save)
- **Files to modify:** `entrypoints/telegram_bot.py` (rating callback, text handler, consent callback)

### Wave 3 (depends on Wave 2 — Trial + Survey)

#### Task 8: Trial system — counter, exhaustion check, survey trigger
- **Files to modify:** `entrypoints/telegram_bot.py` (trial check in voice handler)
- **Files to modify:** `core/db_ops.py` (decrement_trial, complete_survey, block_survey)

#### Task 9: Survey flow — 4 questions, sanity check, 2-strike rule, fail-open
- **Files to modify:** `entrypoints/telegram_bot.py` (survey answer handler)

### Wave 4 (Testing + Polish)

#### Task 10: Unit tests — db_ops, task_extractor, debounce buffer, survey logic
- **Files to create:** `tests/test_db_ops.py`, `tests/test_task_extractor.py`, `tests/test_debounce.py`, `tests/test_survey.py`

#### Task 11: Integration tests — full pipeline with both actions, trial lifecycle, multi-voice
- **Files to create:** `tests/test_integration.py`

#### Task 12: E2E smoke test plan + deployment
- **Files to create:** `docs/e2e-checklist.md`
- **Files to modify:** `systemd/said-done-bot.service`
