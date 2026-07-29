"""The model behind the assistant, kept behind one small door.

Groq today because it is fast and cheap; the choice is not load-bearing. Every
caller goes through `complete()`, so swapping provider is one file and no
feature has to know which model answered.

Two rules the rest of the code depends on.

**No key is not an error.** The assistant is an addition to a directory that
works without it, so an unconfigured deployment must degrade to "unavailable"
rather than 500. `is_configured()` lets a caller decide before it promises a
user anything.

**Nothing here decides medicine.** The model routes a described complaint to a
specialty and drafts translations; the search that follows is ordinary SQL over
verified data. That boundary is deliberate — a hallucinated cardiologist is a
bug, a hallucinated diagnosis is a harm — and it is why the prompts below refuse
to diagnose and why urgent wording short-circuits to emergency numbers before a
model is ever called.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = os.environ.get("SEHATY_LLM_MODEL", "llama-3.3-70b-versatile")
# Identifies us to the provider's edge. Anonymous clients get filtered.
USER_AGENT = "Sehaty/1.0 (+https://sehaty.ma)"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
REQUEST_TIMEOUT = 30


class LLMUnavailable(RuntimeError):
    """No key, or the provider refused. Callers degrade, they do not crash."""


def api_key() -> str | None:
    return os.environ.get("GROQ_API_KEY") or None


def is_configured() -> bool:
    """Whether a key is present. Says nothing about whether it works.

    Checked before a screen offers a chat box, so a user is never invited to
    type a question into something with nothing behind it. Use `diagnose()` to
    find out whether the provider actually answers — a wrong key or a retired
    model leaves this returning True while every call fails.
    """
    return api_key() is not None


def diagnose() -> tuple[bool, str]:
    """Ask the provider one trivial question and report what came back.

    Exists because "configured" and "working" are different states and the
    difference was invisible: a key set to something the provider rejects left
    the status endpoint reporting available, every triage silently falling back
    to a generalist, and nothing anywhere saying why.

    Returns (working, detail). Never raises — it is a diagnostic, and one that
    throws is no use to whoever is trying to find out what is wrong.
    """
    if api_key() is None:
        return False, "GROQ_API_KEY is not set"
    try:
        answer = complete(
            system="Reply with the single word: ok",
            user="ping",
            max_tokens=8,
            temperature=0.0,
        )
    except LLMUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"model {DEFAULT_MODEL} replied {answer[:40]!r}"


def complete(
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tokens: int = 700,
    json_mode: bool = False,
) -> str:
    """One turn. Returns the assistant's text, or raises LLMUnavailable.

    Deliberately not streaming and not stateful: every feature here is a single
    question with a single answer, and a conversation store is a thing to add
    when something needs one rather than on the chance that it might.
    """
    key = api_key()
    if key is None:
        raise LLMUnavailable("GROQ_API_KEY is not set")

    payload: dict = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Low by default: these prompts route and translate, and invention is
        # the failure mode, not dullness.
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    request = urllib.request.Request(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Groq sits behind Cloudflare, which rejects urllib's default
            # "Python-urllib/3.x" signature with 403 error 1010. Without this the
            # key looks wrong and every triage falls back to a generalist, which
            # is the one failure the fallback is designed to be indistinguishable
            # from.
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:  # pragma: no cover - provider-dependent
        # Read the body: Groq explains a retired model or a rejected key there,
        # and "provider returned 400" on its own sends you looking in the wrong
        # place for an afternoon.
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:  # pragma: no cover
            detail = ""
        raise LLMUnavailable(f"provider returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover
        raise LLMUnavailable("provider unreachable") from exc

    choices = body.get("choices") or []
    if not choices:
        raise LLMUnavailable("provider returned no completion")
    return (choices[0].get("message") or {}).get("content", "").strip()


def complete_json(*, system: str, user: str, **kwargs) -> dict:
    """A completion parsed as an object, or LLMUnavailable.

    Malformed JSON is treated as unavailability rather than surfaced: a caller
    that asked for structure cannot do anything useful with prose, and every
    call site here has a working non-AI fallback to fall back to.
    """
    raw = complete(system=system, user=user, json_mode=True, **kwargs)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable("provider returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMUnavailable("provider returned a non-object")
    return parsed
