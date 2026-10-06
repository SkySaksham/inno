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

For the "cmd" backend (Copilot CLI), authentication:
  The Copilot CLI reads GH_TOKEN or COPILOT_GITHUB_TOKEN from the
  environment.  In GitHub Actions the built-in GITHUB_TOKEN is available
  automatically — this module propagates it to GH_TOKEN so Copilot
  authenticates without interactive login or extra secrets.

Thread-safe: every call_llm() invocation is independent.
"""
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

TIMEOUT_S = int(os.environ.get("INNO_LLM_TIMEOUT", "120"))

# Default models per backend
_DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}

# Substrings in stderr that indicate an authentication / credential failure
# rather than a transient or parse error.  Matched case-insensitively.
_AUTH_FAILURE_MARKERS = [
    "no authentication",
    "authentication failed",
    "authentication information",
    "could not authenticate",
    "token is invalid",
    "token expired",
    "unauthorized",
    "401",
    "403",
    "/login",
    "copilot_github_token",
    "gh_token",
]


class AuthenticationError(RuntimeError):
    """Raised when the LLM backend fails due to missing or bad credentials.

    Callers should NOT retry this — it will never succeed without
    reconfiguring the environment.
    """


def is_auth_error(error: BaseException) -> bool:
    """Return True if `error` looks like an authentication failure."""
    if isinstance(error, AuthenticationError):
        return True
    msg = str(error).lower()
    return any(marker in msg for marker in _AUTH_FAILURE_MARKERS)


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
        raise AuthenticationError(
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
        err_body = exc.read().decode()[:300]
        if exc.code in (401, 403):
            raise AuthenticationError(
                f"OpenAI authentication failed (HTTP {exc.code}): {err_body}"
            ) from exc
        raise RuntimeError(
            f"OpenAI API error {exc.code}: {err_body}"
        ) from exc

    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise AuthenticationError(
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
        err_body = exc.read().decode()[:300]
        if exc.code in (401, 403):
            raise AuthenticationError(
                f"Gemini authentication failed (HTTP {exc.code}): {err_body}"
            ) from exc
        raise RuntimeError(
            f"Gemini API error {exc.code}: {err_body}"
        ) from exc

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc


# ---------------------------------------------------------------------------
# Subprocess (copilot / any CLI) backend
# ---------------------------------------------------------------------------

def _cmd_env() -> dict[str, str]:
    """Build the environment dict for the cmd subprocess.

    Propagates GITHUB_TOKEN → GH_TOKEN so the Copilot CLI can
    authenticate in GitHub Actions without requiring a separate secret.
    """
    env = os.environ.copy()
    gh_token = env.get("GITHUB_TOKEN", "")
    if gh_token:
        # GH_TOKEN is what Copilot CLI checks first
        env.setdefault("GH_TOKEN", gh_token)
        env.setdefault("COPILOT_GITHUB_TOKEN", gh_token)
    return env


def _call_cmd(prompt: str) -> str:
    cmd_str = os.environ.get("INNO_LLM_CMD", "copilot -p")
    res = subprocess.run(
        shlex.split(cmd_str) + [prompt],
        capture_output=True, text=True, timeout=TIMEOUT_S,
        env=_cmd_env(),
    )
    if res.returncode != 0:
        stderr = res.stderr.strip()[:400]
        # Detect auth failures so callers don't pointlessly retry
        if any(m in stderr.lower() for m in _AUTH_FAILURE_MARKERS):
            raise AuthenticationError(
                f"Copilot CLI authentication failed: {stderr}"
            )
        raise RuntimeError(
            f"LLM command failed (exit {res.returncode}): {stderr}"
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

    Raises:
      AuthenticationError  — credentials missing or rejected (do NOT retry)
      RuntimeError         — transient / unexpected failure   (retry is OK)
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


def preflight_check() -> None:
    """Run a trivial LLM call to verify authentication works.

    Call this once before dispatching a batch of concurrent requests.
    Raises AuthenticationError with a clear message if auth is broken,
    so the caller can fail fast instead of spamming N identical errors.

    For the mock backend this is a no-op (always succeeds).
    """
    backend = active_backend()
    if backend == "mock":
        return

    print(f"Preflight: verifying {backend} authentication...", flush=True)
    try:
        # A tiny prompt that every backend can answer quickly
        result = call_llm('Respond with exactly: {"ok":true}')
        if not result or not result.strip():
            raise RuntimeError("LLM returned an empty response.")
    except AuthenticationError:
        raise   # already the right type
    except RuntimeError as exc:
        # Re-check whether this was actually an auth problem
        if is_auth_error(exc):
            raise AuthenticationError(str(exc)) from exc
        raise
    print(f"Preflight: {backend} authentication OK.", flush=True)

