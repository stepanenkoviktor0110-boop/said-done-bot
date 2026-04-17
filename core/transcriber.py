"""HTTP transcription client for the shared Whisper STT service.

Sends audio bytes to the whisper-stt server and returns transcribed text.
Public API is unchanged: ``async def transcribe_ogg(ogg_bytes: bytes) -> str``.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

WHISPER_URL = "http://127.0.0.1:8765/transcribe"

TIMEOUT = httpx.Timeout(120.0, connect=5.0)

_BACKOFF_SCHEDULE = [3, 6, 12]  # seconds between retries

_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout)


class TranscriptionError(Exception):
    """Raised when the whisper service returns an unusable response."""


def _parse_response(response: httpx.Response) -> str:
    """Validate an HTTP response from the whisper server and return text.

    Raises ``TranscriptionError`` on any problem (HTTP error, empty text,
    non-JSON body).
    """
    if response.status_code != 200:
        logger.error(
            "Whisper server returned HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )
        raise TranscriptionError(
            f"Whisper server error: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except Exception as exc:
        logger.error("Failed to parse whisper response as JSON: %s", exc)
        raise TranscriptionError("Corrupted response from whisper server") from exc

    text = data.get("text")
    if not isinstance(text, str) or text.strip() == "":
        logger.error("Whisper server returned empty text: %s", data)
        raise TranscriptionError("Whisper server returned empty transcription")

    return text.strip()


async def transcribe_ogg(ogg_bytes: bytes) -> str:
    """Send OGG audio bytes to the whisper service and return the transcription.

    Retries up to 3 times (4 total attempts) on connection errors with
    backoff delays of 3, 6, 12 seconds.  Does **not** retry on read
    timeouts or HTTP 4xx/5xx.
    """
    last_exc: BaseException | None = None

    for attempt in range(len(_BACKOFF_SCHEDULE) + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(
                    WHISPER_URL,
                    files={"file": ("audio.ogg", ogg_bytes, "audio/ogg")},
                )
            return _parse_response(response)

        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < len(_BACKOFF_SCHEDULE):
                delay = _BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Whisper connection failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1,
                    len(_BACKOFF_SCHEDULE) + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Whisper connection failed after %d attempts: %s",
                    len(_BACKOFF_SCHEDULE) + 1,
                    exc,
                )

    raise TranscriptionError(
        f"Whisper server unavailable after {len(_BACKOFF_SCHEDULE) + 1} attempts"
    ) from last_exc
