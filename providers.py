"""
providers.py — Shared LLM provider registry and single-shot call helpers.

Used by ontology_miner.py and ontology_agent.py. Handles the six supported
providers: anthropic, openai, gemini, mistral, ollama, openai-compat.
"""

import os
import sys
from urllib.parse import quote

import requests

PROVIDERS: dict[str, dict] = {
    "anthropic":    {"label": "Anthropic (Claude)",         "default_model": "claude-sonnet-4-6",    "env_key": "ANTHROPIC_API_KEY"},
    "openai":       {"label": "OpenAI",                     "default_model": "gpt-4o",               "env_key": "OPENAI_API_KEY"},
    "gemini":       {"label": "Google Gemini",              "default_model": "gemini-2.0-flash",     "env_key": "GEMINI_API_KEY"},
    "mistral":      {"label": "Mistral AI",                 "default_model": "mistral-large-latest", "env_key": "MISTRAL_API_KEY"},
    "ollama":       {"label": "Ollama (local)",             "default_model": "llama3.2",             "env_key": None},
    "openai-compat":{"label": "OpenAI-compatible endpoint", "default_model": os.getenv("OPENAI_COMPAT_MODEL", "default"), "env_key": None},
}

# (url, api_key_env) — None url means read from OPENAI_COMPAT_BASE_URL
_PROVIDER_BASES: dict[str, tuple[str | None, str | None]] = {
    "openai":        ("https://api.openai.com", "OPENAI_API_KEY"),
    "mistral":       ("https://api.mistral.ai", "MISTRAL_API_KEY"),
    "ollama":        ("http://localhost:11434",  None),
    "openai-compat": (None,                     "OPENAI_COMPAT_API_KEY"),
}


def resolve_provider_base(provider_key: str) -> tuple[str, str | None]:
    """Return (base_url, api_key) for any OpenAI-compat provider. Single source of truth shared by call_plain_llm and run_agent."""
    if provider_key not in _PROVIDER_BASES:
        raise KeyError(f'Unknown provider "{provider_key}" for OpenAI-compat base resolution')
    url, key_env = _PROVIDER_BASES[provider_key]
    base_url = url if url is not None else (os.getenv("OPENAI_COMPAT_BASE_URL") or "")
    api_key  = os.getenv(key_env) if key_env else None
    return base_url, api_key


def validate_provider(provider_key: str) -> dict:
    """Check provider exists and required env vars are set. Exits on error."""
    if provider_key not in PROVIDERS:
        print(f'Unknown provider "{provider_key}". Run --providers to list options.', file=sys.stderr)
        sys.exit(1)
    cfg = PROVIDERS[provider_key]
    if cfg["env_key"] and not os.getenv(cfg["env_key"]):
        print(f"Error: {cfg['env_key']} is not set.", file=sys.stderr)
        sys.exit(1)
    if provider_key == "openai-compat" and not os.getenv("OPENAI_COMPAT_BASE_URL"):
        print("Error: OPENAI_COMPAT_BASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    return cfg


def _fmt_request_error(e: requests.RequestException, label: str) -> str:
    """Format a requests exception into a short, actionable error string."""
    if isinstance(e, requests.ConnectionError):
        local_hint = " — is Ollama running?" if "localhost" in label or "127.0.0.1" in label else " — check your network connection"
        return f"Could not connect to {label}{local_hint}"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in (401, 403):
            return f"{label} authentication failed (HTTP {status}) — check your API key"
        if status == 429:
            return f"{label} rate limit exceeded (HTTP 429) — wait and retry"
        return f"{label} returned HTTP {status}"
    if isinstance(e, requests.Timeout):
        return f"{label} request timed out — try again or increase timeout"
    return f"LLM request failed ({label}): {str(e).splitlines()[0]}"


def _call_openai_compat_plain(base_url: str, api_key: str | None, system: str, user: str, model: str) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.post(
            f"{base_url}/v1/chat/completions",
            headers=headers,
            json={"model": model, "max_tokens": 4096, "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ]},
            timeout=120,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(_fmt_request_error(e, base_url)) from e
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def call_plain_llm(system: str, user: str, provider_key: str, model: str) -> str:
    """Single-shot LLM call with no tools."""
    if provider_key == "anthropic":
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={"model": model, "max_tokens": 4096, "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(_fmt_request_error(e, "Anthropic")) from e
        content = r.json().get("content") or []
        return next((c["text"] for c in content if c.get("type") == "text"), "")

    if provider_key == "gemini":
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model)}:generateContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": 4096},
                },
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(_fmt_request_error(e, "Gemini")) from e
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(p["text"] for p in parts if "text" in p)

    base_url, api_key = resolve_provider_base(provider_key)
    return _call_openai_compat_plain(base_url, api_key, system, user, model)
