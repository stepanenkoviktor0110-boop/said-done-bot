"""Task extraction from transcribed voice via LLM."""

import re
from pathlib import Path

MAX_TRANSCRIPT = 4000
_prompt_cache: str | None = None


def _get_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is not None:
        return _prompt_cache
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "task-extraction.md"
    _prompt_cache = prompt_path.read_text(encoding="utf-8")
    return _prompt_cache


def _parse_response(text: str) -> dict:
    t = text.strip()

    if t.startswith("__NO_TASKS__"):
        return {"tasks": [], "marker": "no_tasks"}
    if t.startswith("__TOO_MANY_TASKS__"):
        return {"tasks": [], "marker": "too_many_tasks"}
    if t.startswith("__SUMMARY__"):
        return {"tasks": [], "marker": "summary",
                "summary": t[len("__SUMMARY__"):].strip()}

    tasks = [m.group(1).strip() for m in re.finditer(
        r"^\s*\d+\.\s+(.+)$", t, re.MULTILINE
    ) if m.group(1).strip()]

    if tasks:
        return {"tasks": tasks, "marker": None}

    return {"tasks": [], "marker": None, "error": "format_error"}


async def extract_tasks(transcripts: list[str], api_key: str, model: str) -> dict:
    """Extract tasks from transcript(s) via OpenRouter LLM."""
    import httpx

    combined = "\n".join(transcripts)
    truncated = False
    if len(combined) > MAX_TRANSCRIPT:
        combined = combined[:MAX_TRANSCRIPT]
        truncated = True

    system_prompt = _get_prompt()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://said-done-bot",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": combined},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

    result = _parse_response(content)
    result["truncated"] = truncated
    return result
