#!/usr/bin/env python3
"""Unified LLM engine: codex exec (128K) → call_llm fallback (auto-batch).

All cmd_* functions in ai_report.py call exactly one function: call_engine().
This module handles engine selection, context limits, and batching internally.
"""

import json, os, re, shutil, subprocess, sys, time
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def load_dotenv(path: Path = None):
    """Minimal .env loader — no dependencies."""
    p = path or Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Low-level backends
# ---------------------------------------------------------------------------

def call_llm(prompt: str, system: str = "", max_tokens: int = None) -> str:
    """OpenAI-compatible HTTP call with retry for transient errors.

    Retries on: HTTP 429/5xx, URLError (timeout/connection), JSON parse errors.
    Exits non-zero on: auth failure (401/403), missing API key, max retries exhausted.
    """
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("LLM_API_KEY not set", file=sys.stderr); sys.exit(1)
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL_NAME", "deepseek-v4-flash")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    if max_tokens is None:
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", 2000))
    payload = json.dumps({"model": model, "messages": msgs, "max_tokens": max_tokens}).encode()

    max_retries = int(os.environ.get("LLM_MAX_RETRIES", 3))
    for attempt in range(max_retries + 1):
        req = Request(f"{base}/chat/completions", data=payload,
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"].get("content") or ""
        except HTTPError as e:
            body = e.read().decode()[:200]
            # Auth errors: never retry, fail immediately
            if e.code in (401, 403):
                print(f"LLM auth error {e.code}: {body}", file=sys.stderr); sys.exit(1)
            # Retryable: 429 rate limit, 5xx server errors
            if e.code == 429 or e.code >= 500:
                if attempt < max_retries:
                    delay = 2 ** attempt
                    print(f"LLM {e.code} retry {attempt+1}/{max_retries} after {delay}s: {body}", file=sys.stderr)
                    time.sleep(delay)
                    continue
            # Non-retryable HTTP error or retries exhausted
            print(f"LLM API error: {e.code} {body}", file=sys.stderr); sys.exit(1)
        except (URLError, json.JSONDecodeError, KeyError) as e:
            # Network timeout, connection error, malformed response
            if attempt < max_retries:
                delay = 2 ** attempt
                print(f"LLM transient error retry {attempt+1}/{max_retries} after {delay}s: {type(e).__name__}: {e}", file=sys.stderr)
                time.sleep(delay)
                continue
            print(f"LLM transient error (retries exhausted): {type(e).__name__}: {e}", file=sys.stderr); sys.exit(1)


def _call_codex(content: str, system: str) -> str:
    """Call codex exec, retrying once before using the HTTP fallback."""
    cmd = ["codex", "exec", "--ephemeral", system]
    for attempt in range(2):
        try:
            result = subprocess.run(
                cmd, input=content, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            print(f"codex exec timed out (attempt {attempt + 1}/2)", file=sys.stderr)
        else:
            if result.returncode == 0:
                return result.stdout.strip()
            # Codex prints a long startup banner before the actionable error.
            # Keep the tail so cron logs preserve the actual root cause.
            detail = result.stderr.strip()[-2000:]
            print(
                f"codex exec failed ({result.returncode}, attempt {attempt + 1}/2): {detail}",
                file=sys.stderr,
            )
        if attempt == 0:
            time.sleep(2)
    return ""


@lru_cache(maxsize=1)
def _codex_available() -> bool:
    return shutil.which("codex") is not None


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def call_engine(content: str, system: str, max_tokens: int = 4000, allow_chunking: bool = True) -> str:
    """Unified LLM call. All cmd_* functions use this and only this.

    Strategy:
      1. codex exec available? → 128K context, single call, full content
      2. fallback → call_llm with auto-batching for small-context models

    allow_chunking=False disables the auto-batch split-and-join fallback below.
    Only safe to leave True for prompts that extract independent per-chunk items
    (e.g. bullet extraction) — chunk outputs are validly concatenable. Prompts
    that ask the LLM to produce ONE complete, non-repeating document (e.g. a
    holistic rewrite/persona synthesis) MUST pass allow_chunking=False, or a
    chunked call produces N complete-but-conflicting documents joined together.
    """
    if _codex_available():
        result = _call_codex(content, system)
        if result:
            return result
        print("codex exec failed, falling back to call_llm", file=sys.stderr)
    return _call_llm_auto(content, system, max_tokens, allow_chunking)


def _call_llm_auto(content: str, system: str, max_tokens: int, allow_chunking: bool = True) -> str:
    """call_llm with conservative auto-batching for fallback models."""
    MAX_PROMPT_CHARS = 6000
    if len(content) + len(system) < MAX_PROMPT_CHARS or not allow_chunking:
        return call_llm(content, system, max_tokens)

    # Split by natural section breaks (---), accumulate chunks under budget
    sections = re.split(r'\n---\n', content)
    budget = MAX_PROMPT_CHARS - len(system) - 500
    chunks, cur = [], ""
    for sec in sections:
        if len(cur) + len(sec) > budget:
            if cur:
                chunks.append(cur)
            cur = sec
        else:
            cur = cur + "\n---\n" + sec if cur else sec
    if cur:
        chunks.append(cur)

    parts = [call_llm(chunk, system, max_tokens) for chunk in chunks]
    return "\n".join(p for p in parts if p)
