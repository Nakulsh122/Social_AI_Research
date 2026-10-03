import asyncio
import os

import httpx


DEFAULT_URL = "https://text.api.pangram.com/v3"
TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0


class PangramError(RuntimeError):
    pass


async def score_text(text: str) -> dict:
    api_key = os.environ.get("PANGRAM_API_KEY", "").strip()
    api_url = os.environ.get("PANGRAM_API_URL", DEFAULT_URL).strip()
    if not api_key:
        raise PangramError("PANGRAM_API_KEY missing in environment")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                res = await client.post(
                    api_url,
                    headers={"x-api-key": api_key, "content-type": "application/json"},
                    json={"text": text},
                )
        except httpx.HTTPError as e:
            if attempt >= MAX_ATTEMPTS:
                raise PangramError(f"network error calling Pangram: {e}") from e
            await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue

        body_text = res.text
        try:
            parsed = res.json()
        except ValueError:
            parsed = {"raw": body_text}

        if 500 <= res.status_code < 600:
            msg = (
                parsed.get("message")
                or parsed.get("error")
                or parsed.get("msg")
                or body_text[:200]
            ) if isinstance(parsed, dict) else body_text[:200]
            if attempt >= MAX_ATTEMPTS:
                raise PangramError(f"Pangram {res.status_code}: {msg}")
            await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue

        if res.status_code >= 400:
            msg = (
                parsed.get("message")
                or parsed.get("error")
                or parsed.get("msg")
                or body_text[:200]
            ) if isinstance(parsed, dict) else body_text[:200]
            raise PangramError(f"Pangram {res.status_code}: {msg}")

        break

    # Per-token arrays bloat each record by 5-10x. We keep `windows[]` for
    # segment-level analysis and `fraction_ai` for overall; the original `text`
    # is preserved on the scored_posts record so per-token info can be
    # re-derived by re-querying Pangram if ever needed.
    if isinstance(parsed, dict):
        parsed.pop("tokens", None)
        parsed.pop("token_probs", None)
    return parsed
