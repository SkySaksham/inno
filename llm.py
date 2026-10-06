#!/usr/bin/env python3
"""
llm.py  —  Inno shared LLM backend.

Selects the backend from the INNO_LLM_BACKEND environment variable:

  mock      dry-run, returns empty findings / false positives (default when no key found)
  openai    calls OpenAI chat completions API  (needs OPENAI_API_KEY)
  gemini    calls Google Gemini API            (needs GEMINI_API_KEY)
  cmd       spawns INNO_LLM_CMD as a subprocess, appends the prompt as last arg
            (original copilot behaviour)

Additional env vars:
  INNO_LLM_MODEL    model name override (e.g. "gpt-4o", "gemini-2.0-flash")
  INNO_LLM_TIMEOUT  per-call timeout in seconds (default 120)
  INNO_LLM_CMD      command string for the "cmd" backend (default: "copilot -p")

Thread-safe: every call_llm() invocation is independent.
"""
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request

TIMEOUT_S = int(os.environ.get("INNO_LLM_TIMEOUT", "120"))

# Default models per backend
_DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

def _mock_response(context_hint: str = "") -> str:
    """Return a minimal valid JSON string that passes both review schemas."""
    # ai_review schema
    if "is_real_issue" in context_hint or not context_hint:
        return json.dumps({
            "is_real_issue": True,
            "severity": "low",
            "reason": "[mock] Not analyzed — running in mock mode.",
            "impact": "[mock] Unknown.",
            "suggested_fix": "",
            "confidence": "low",
        })
    # semantic_review schema
    return json.dumps({"findings": []})


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------

def _call_openai(prompt: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "INNO_LLM_BACKEND=openai but OPENAI_API_KEY is not set."
        )
    model = os.environ.get("INNO_LLM_MODEL", _DEFAULT_MODEL["openai"])
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"OpenAI API error {exc.code}: {exc.read().decode()[:300]}"
        ) from exc

    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "INNO_LLM_BACKEND=gemini but GEMINI_API_KEY is not set."
        )
    model = os.environ.get("INNO_LLM_MODEL", _DEFAULT_MODEL["gemini"])
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
    }).encode()

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Gemini API error {exc.code}: {exc.read().decode()[:300]}"
        ) from exc

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc


# ---------------------------------------------------------------------------
# Subprocess (copilot / any CLI) backend
# ---------------------------------------------------------------------------

def _call_cmd(prompt: str) -> str:
    cmd_str = os.environ.get("INNO_LLM_CMD", "copilot -p")
    res = subprocess.run(
        shlex.split(cmd_str) + [prompt],
        capture_output=True, text=True, timeout=TIMEOUT_S,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"LLM command failed (exit {res.returncode}): "
            f"{res.stderr.strip()[:300]}"
        )
    return res.stdout


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(prompt: str, context_hint: str = "") -> str:
    """
    Call the configured LLM backend and return raw text.

    context_hint is used only by the mock backend to decide which schema
    to return; pass "semantic" for semantic_review calls.

    Raises RuntimeError on hard failures (caller should retry or warn).
    """
    backend = os.environ.get("INNO_LLM_BACKEND", "").lower().strip()

    # Auto-detect if backend not set
    if not backend:
        if os.environ.get("OPENAI_API_KEY"):
            backend = "openai"
        elif os.environ.get("GEMINI_API_KEY"):
            backend = "gemini"
        elif os.environ.get("INNO_LLM_CMD"):
            backend = "cmd"
        else:
            backend = "mock"

    if backend == "mock":
        return _mock_response(context_hint)
    if backend == "openai":
        return _call_openai(prompt)
    if backend == "gemini":
        return _call_gemini(prompt)
    if backend == "cmd":
        return _call_cmd(prompt)

    raise RuntimeError(
        f"Unknown INNO_LLM_BACKEND={backend!r}. "
        "Valid values: openai, gemini, cmd, mock"
    )


def active_backend() -> str:
    """Return the name of the backend that would be used right now."""
    b = os.environ.get("INNO_LLM_BACKEND", "").lower().strip()
    if b:
        return b
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("INNO_LLM_CMD"):
        return "cmd"
    return "mock"

