#!/usr/bin/env python3
"""ai_smoketest.py

Purpose
  Verify OpenAI connectivity + prompt/response shape for the JustWordle v3 insights layer
  WITHOUT posting anything to Slack and WITHOUT requiring Google/Slack credentials.

What it does
  - Loads .env from this script's folder (same behavior as the bot).
  - Builds a small deterministic facts bundle (or loads one from a JSON file).
  - Renders the template fallback recap using insights.py.
  - If AI rewrite is enabled, calls OpenAI and prints output to the terminal only.

API selection
  OPENAI_API_MODE (or --api) can be:
    - auto (default): Responses for GPT-5/GPT-6 reasoning models, Chat Completions otherwise
    - responses: always use /v1/responses
    - chat: always use /v1/chat/completions

Reasoning effort
  OPENAI_REASONING_EFFORT (or --reasoning-effort) controls reasoning models.
  GPT-5 defaults to minimal and GPT-6 defaults to low reasoning effort.

Examples
  python ai_smoketest.py
  python ai_smoketest.py --facts facts.json
  python ai_smoketest.py --ai --model gpt-5-mini --api responses
  python ai_smoketest.py --ai --style flair
  python ai_smoketest.py --ai --model gpt-5-mini --reasoning-effort minimal
  python ai_smoketest.py --no-ai

Notes
  - This script intentionally does NOT import score-bot.py (avoids SPREADSHEET_ID creds).
  - It also does NOT swallow HTTP errors; you'll see status + response body.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai_compat import (
    chat_completion_token_limit_param as _chat_completion_token_limit_param,
    default_reasoning_effort as _default_reasoning_effort,
    model_prefers_responses as _model_prefers_responses,
    supports_temperature as _supports_temperature,
)
from insights import _collapse_blank_lines


DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 220
DEFAULT_TIMEOUT_S = 15.0


def _load_dotenv_local() -> None:
    """Load .env from this script's directory (not the current working directory)."""
    try:
        from dotenv import load_dotenv  # type: ignore

        base = Path(__file__).resolve().parent
        if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
            load_dotenv(dotenv_path=base / ".env", override=False)
    except Exception:
        # If python-dotenv isn't available, we still allow env vars via OS.
        pass


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "(missing)"
    if len(k) <= 10:
        return "(set)"
    return f"{k[:3]}...{k[-4:]}"


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _get_env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _get_env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
        return v if v > 0 else default
    except Exception:
        return default


def _normalize_api_mode(v: str) -> str:
    v = str(v or "").strip().lower()
    if v in ("responses", "response"):
        return "responses"
    if v in ("chat", "chatcompletions", "chat_completions"):
        return "chat"
    return "auto"


def _resolve_api_mode(model: str, requested: str) -> Tuple[str, str]:
    """Return (resolved_mode, endpoint_path)."""
    mode = _normalize_api_mode(requested)
    if mode == "responses":
        return "responses", "/v1/responses"
    if mode == "chat":
        return "chat", "/v1/chat/completions"

    if _model_prefers_responses(model):
        return "responses", "/v1/responses"
    return "chat", "/v1/chat/completions"


def _default_facts(day: str) -> Dict[str, Any]:
    # Keep these user IDs obviously fake but Slack-mention safe.
    return {
        "day": day,
        "players": ["U111AAA", "U222BBB", "U333CCC", "U444DDD"],
        "expected_players": 4,
        "complete_players": 4,
        "mvp": {"user_id": "U111AAA", "wins": 2, "ties": 1},
        "tightest_race": {
            "game": "Zip",
            "winner_uid": "U111AAA",
            "runner_up_uid": "U222BBB",
            "best_value": 8,
            "runner_up_value": 9,
            "margin": 1,
            "metric_type": "time",
            "best_display": "0:08",
        },
        "blowout": {
            "game": "Pinpoint",
            "winner_uid": "U333CCC",
            "runner_up_uid": "U444DDD",
            "best_value": 1,
            "runner_up_value": 4,
            "margin": 3,
            "metric_type": "guesses",
            "best_display": "1",
        },
    }


def _read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("facts JSON must be an object")
    return obj


def _get_prompts(kind: str, facts: Dict[str, Any], fallback_text: str) -> Tuple[str, str, Dict[str, Any]]:
    """Return (system_prompt, user_prompt, extra_fields) using insights.py.

    This keeps the smoketest aligned with whatever prompt profile the bot is using.
    """
    try:
        from insights import build_ai_prompts  # local module

        return build_ai_prompts(kind, facts, fallback_text)
    except Exception:
        # Fallback: basic rewrite prompts.
        sys_prompt = (
            "You write short Slack recaps for a friendly puzzle league. "
            "Use only the provided facts. Do not invent winners, scores, or players. "
            "Keep it under 8 lines. Avoid hashtags."
            "Use your best sports announcer voice to add colour commentary to the recap."
            "The tagline (but never label it as 'Tagline') should be adjusted based on the recap to increase the snark level for any dramatic wins, losses, or close games."
        )
        user_prompt = (
            f"Rewrite this {kind} message to be punchy and readable. "
            f"Return plain text only.\n\n"
            f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"Fallback:\n{fallback_text}"
        )
        return sys_prompt, user_prompt, {}


def _build_chat_body(
    *,
    model: str,
    sys_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: Optional[str],
    include_temperature: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        _chat_completion_token_limit_param(model): max_tokens,
    }
    if include_temperature:
        body["temperature"] = temperature
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    return body

def _build_responses_body(
    *,
    model: str,
    sys_prompt: str,
    user_prompt: str,
    extra_fields: Dict[str, Any],
    temperature: float,
    max_tokens: int,
    reasoning_effort: Optional[str],
    include_temperature: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "instructions": sys_prompt,
        "input": user_prompt,
        "max_output_tokens": max_tokens,
    }
    # Optional per-profile knobs (e.g., text verbosity).
    if isinstance(extra_fields, dict):
        for k, v in extra_fields.items():
            if k not in body:
                body[k] = v
    if include_temperature:
        body["temperature"] = temperature
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    return body

def _extract_text_from_content_parts(parts: Any) -> str:
    """Extract text from Chat Completions 'content' or Responses 'content' parts."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    out: List[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("text", "output_text", "input_text"):
            txt = p.get("text")
            if isinstance(txt, str) and txt:
                out.append(txt)
    return "".join(out)


def _extract_text_from_openai_payload(payload: Any) -> Tuple[str, str]:
    """Return (text, diagnostic). diagnostic is empty when text is found."""
    if not isinstance(payload, dict):
        return "", "response was not a JSON object"

    if isinstance(payload.get("error"), dict):
        return "", f"API error: {payload.get('error')}"

    # Responses API shape
    ot = payload.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot.strip(), ""

    if isinstance(payload.get("output"), list):
        texts: List[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            texts.append(_extract_text_from_content_parts(item.get("content")))
        joined = "".join(texts).strip()
        if joined:
            return joined, ""

    # Chat Completions shape
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        ch0 = choices[0] if isinstance(choices[0], dict) else {}
        finish = ch0.get("finish_reason")
        msg = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}

        refusal = msg.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return "", f"Model refusal: {refusal.strip()}"

        content = msg.get("content")
        text = _extract_text_from_content_parts(content).strip()
        if text:
            return text, ""

        # Some providers stick plain text under msg['text']
        alt = msg.get("text")
        if isinstance(alt, str) and alt.strip():
            return alt.strip(), ""

        tool_calls = msg.get("tool_calls")
        if finish in ("tool_calls", "function_call"):
            return "", f"finish_reason={finish} (assistant attempted tool calls), tool_calls={bool(tool_calls)}"
        if finish == "content_filter":
            return "", "finish_reason=content_filter (content omitted)"

        return "", f"empty assistant message content (finish_reason={finish})"

    return "", "unrecognized response shape (no choices/output/output_text)"


# --- diagnostics helpers (Slack mention + formatting validation) ---

_UID_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9]{6,})\b")
_SLACK_MENTION_RE = re.compile(r"<@([A-Z][A-Z0-9]{6,})>")
# Raw @UID mentions that are NOT already inside <@...>
_RAW_AT_UID_RE = re.compile(r"(?<!<)@([A-Z][A-Z0-9]{6,})\b")


def _norm_uid(x: Any) -> str:
    s = str(x or "").strip()
    # Accept: U123..., <@U123...>, @U123...
    if s.startswith('<@') and s.endswith('>'):
        s = s[2:-1].strip()
    if s.startswith('@'):
        s = s[1:].strip()
    return s


def _expected_uids_from_facts(facts: Dict[str, Any]) -> List[str]:
    uids = set()

    players = facts.get('players')
    if isinstance(players, list):
        for p in players:
            u = _norm_uid(p)
            if u:
                uids.add(u)

    mvp = facts.get('mvp')
    if isinstance(mvp, dict):
        u = _norm_uid(mvp.get('user_id'))
        if u:
            uids.add(u)

    for key in ('tightest_race', 'blowout'):
        obj = facts.get(key)
        if not isinstance(obj, dict):
            continue
        for k in ('winner_uid', 'runner_up_uid'):
            u = _norm_uid(obj.get(k))
            if u:
                uids.add(u)

    return sorted(uids)


def _analyze_output_text(text_out: str, expected_uids: List[str]) -> Dict[str, Any]:
    raw = text_out or ''
    slack_mentions = set(_SLACK_MENTION_RE.findall(raw))
    raw_at_mentions = set(_RAW_AT_UID_RE.findall(raw))
    bare_ids = set(_UID_TOKEN_RE.findall(raw))

    expected = set(expected_uids or [])

    # Bare IDs include ones already present in <@...> or @...; we want the ones that appear without mention wrappers.
    bare_only = {u for u in bare_ids if (u not in slack_mentions and u not in raw_at_mentions)}

    missing = sorted(expected - slack_mentions - raw_at_mentions - bare_only)
    present_any = sorted((slack_mentions | raw_at_mentions | bare_only) & expected)
    unexpected = sorted((slack_mentions | raw_at_mentions | bare_only) - expected)

    bad_raw_at = sorted(raw_at_mentions)

    # Formatting signals
    triple_blank = bool(re.search(r"\n\s*\n\s*\n", raw))
    contains_em_dash = '—' in raw

    return {
        'expected': sorted(expected),
        'present_any': present_any,
        'slack_mentions': sorted(slack_mentions),
        'raw_at_mentions': sorted(raw_at_mentions),
        'bare_ids': sorted(bare_only),
        'missing': missing,
        'unexpected': unexpected,
        'bad_raw_at': bad_raw_at,
        'triple_blank': triple_blank,
        'contains_em_dash': contains_em_dash,
        'line_count': len([ln for ln in raw.splitlines() if ln.strip() != '']),
    }


def _print_diagnostics(diag: Dict[str, Any]) -> None:
    print("\n-- diagnostics --")
    print(f"Non-empty line count: {diag.get('line_count')}")
    if diag.get('contains_em_dash'):
        print("Warning: output contains an em dash (—).")
    if diag.get('triple_blank'):
        print("Warning: output contains 2+ consecutive blank lines (before collapsing).")

    exp = diag.get('expected') or []
    if exp:
        print(f"Expected user_ids: {', '.join(exp)}")
    print(f"Found Slack mentions (<@...>): {', '.join(diag.get('slack_mentions') or []) or '(none)'}")
    raw_at = diag.get('raw_at_mentions') or []
    if raw_at:
        print(f"Found raw @UID (NOT Slack mention syntax): {', '.join(raw_at)}")
        print("Hint: Slack needs <@U123...>, not @U123...")
    bare = diag.get('bare_ids') or []
    if bare:
        print(f"Found bare IDs (no @ / no <@...>): {', '.join(bare)}")

    missing = diag.get('missing') or []
    if missing:
        print(f"Missing expected IDs in output: {', '.join(missing)}")
    unexpected = diag.get('unexpected') or []
    if unexpected:
        print(f"Unexpected IDs in output: {', '.join(unexpected)}")



def _call_openai(
    *,
    endpoint_path: str,
    body: Dict[str, Any],
    print_response: bool = False,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (content, error_text, http_status)."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY is not set.", None

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
    url = f"{base_url}{endpoint_path}"
    timeout_s = _get_env_float("OPENAI_TIMEOUT", DEFAULT_TIMEOUT_S)

    req = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", None)
        if print_response:
            print("-- raw response --")
            print(raw)
            print()

        payload = json.loads(raw)
        text, diag = _extract_text_from_openai_payload(payload)
        if not text:
            return None, diag or "No content returned", status
        return text, None, status
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = "(no response body)"
        return None, f"HTTPError {e.code}: {body_text}", int(e.code)
    except urllib.error.URLError as e:
        return None, f"URLError: {e}", None
    except json.JSONDecodeError as e:
        return None, f"JSONDecodeError: {e}", None
    except Exception as e:
        return None, f"Unexpected error: {type(e).__name__}: {e}", None


def main() -> int:
    _load_dotenv_local()

    ap = argparse.ArgumentParser(description="Smoke-test OpenAI rewrite calls for JustWordle v3 insights.")
    ap.add_argument("--facts", default="", help="Path to a facts JSON file (optional)")
    ap.add_argument("--kind", default="daily recap", help="Rewrite kind label (default: daily recap)")
    ap.add_argument("--day", default="2026-01-22", help="Day string for default facts (default: 2026-01-22)")

    ap.add_argument("--api", default="", choices=["", "auto", "responses", "chat"], help="Override OPENAI_API_MODE")
    ap.add_argument(
        "--style",
        default="",
        choices=["", "basic", "flair"],
        help="Override AI_RECAP_STYLE (basic or flair). Applies when kind contains 'recap'.",
    )
    ap.add_argument("--model", default="", help="Override OPENAI_MODEL for this run")
    ap.add_argument("--base-url", default="", help="Override OPENAI_BASE_URL for this run")
    ap.add_argument("--temperature", default="", help="Override OPENAI_TEMPERATURE for this run")
    ap.add_argument("--max-tokens", default="", help="Override OPENAI_MAX_TOKENS for this run")
    ap.add_argument("--timeout", default="", help="Override OPENAI_TIMEOUT for this run (seconds)")
    ap.add_argument(
        "--reasoning-effort",
        default="",
        help="Override OPENAI_REASONING_EFFORT (model-dependent; e.g. none, low, medium, high)",
    )

    ap.add_argument("--ai", action="store_true", help="Force-enable AI_REWRITE_ENABLED=1")
    ap.add_argument("--no-ai", action="store_true", help="Force-disable AI_REWRITE_ENABLED=0")

    ap.add_argument("--print-facts", action="store_true", help="Print facts JSON used")
    ap.add_argument("--print-request", action="store_true", help="Print the OpenAI request payload")
    ap.add_argument("--print-response", action="store_true", help="Print the full OpenAI JSON response")
    ap.add_argument("--show-raw", action="store_true", help="Also print raw AI text (before blank-line collapsing)")
    ap.add_argument("--debug", action="store_true", help="Enable extra diagnostics (sets OPENAI_REWRITE_DEBUG=1)")
    ap.add_argument("--validate", dest="validate", action="store_true", help="Run Slack mention + formatting checks (default)")
    ap.add_argument("--no-validate", dest="validate", action="store_false", help="Disable Slack mention + formatting checks")
    ap.set_defaults(validate=True)
    args = ap.parse_args()

    if args.debug:
        os.environ["OPENAI_REWRITE_DEBUG"] = "1"
        os.environ["AI_REWRITE_DEBUG"] = "1"
    # Route insights logger to stdout for smoketest visibility.
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")

    if args.ai and args.no_ai:
        print("Pick one: --ai or --no-ai", file=sys.stderr)
        return 2

    if args.ai:
        os.environ["AI_REWRITE_ENABLED"] = "1"
    if args.no_ai:
        os.environ["AI_REWRITE_ENABLED"] = "0"

    # Per-run overrides (do not persist)
    if args.api:
        os.environ["OPENAI_API_MODE"] = args.api
    if args.style:
        os.environ["AI_RECAP_STYLE"] = args.style
    if args.model:
        os.environ["OPENAI_MODEL"] = args.model
    if args.base_url:
        os.environ["OPENAI_BASE_URL"] = args.base_url
    if args.temperature:
        os.environ["OPENAI_TEMPERATURE"] = args.temperature
    if args.max_tokens:
        os.environ["OPENAI_MAX_TOKENS"] = args.max_tokens
    if args.timeout:
        os.environ["OPENAI_TIMEOUT"] = args.timeout
    if args.reasoning_effort:
        os.environ["OPENAI_REASONING_EFFORT"] = args.reasoning_effort

    # Build facts
    if args.facts:
        facts_path = Path(args.facts).expanduser().resolve()
        facts = _read_json(facts_path)
    else:
        facts = _default_facts(args.day)

    # Generate fallback via insights (template)
    try:
        from insights import render_daily_recap_text  # local module

        fallback = render_daily_recap_text(facts)
    except Exception as e:
        fallback = f"(failed to render fallback via insights.py: {type(e).__name__}: {e})"

    enabled = _truthy(os.environ.get("AI_REWRITE_ENABLED", "0"))
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    requested_mode = os.environ.get("OPENAI_API_MODE", "auto")
    resolved_mode, endpoint_path = _resolve_api_mode(model, requested_mode)

    temperature = _get_env_float("OPENAI_TEMPERATURE", DEFAULT_TEMPERATURE)
    max_tokens = _get_env_int("OPENAI_MAX_TOKENS", DEFAULT_MAX_TOKENS)

    reasoning_effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip()
    if not reasoning_effort:
        reasoning_effort = _default_reasoning_effort(model) or ""


    supports_temp = _supports_temperature(model, reasoning_effort)

    print("== OpenAI rewrite smoke test ==")
    print(f"AI_REWRITE_ENABLED: {os.environ.get('AI_REWRITE_ENABLED','0')}  (enabled={enabled})")
    print(f"AI_RECAP_STYLE: {os.environ.get('AI_RECAP_STYLE','(default: basic)')}")
    print(f"OPENAI_API_KEY: {_mask_key(api_key)}")
    print(f"OPENAI_BASE_URL: {base_url}")
    print(f"OPENAI_MODEL: {model}")
    print(f"OPENAI_API_MODE: {requested_mode}  (resolved={resolved_mode}, endpoint={endpoint_path})")
    print(f"OPENAI_TEMPERATURE: {temperature}  (sent={supports_temp})")
    print(f"OPENAI_MAX_TOKENS: {max_tokens}")
    print(f"OPENAI_TIMEOUT: {_get_env_float('OPENAI_TIMEOUT', DEFAULT_TIMEOUT_S)}")
    if reasoning_effort:
        print(f"OPENAI_REASONING_EFFORT: {reasoning_effort}")
    else:
        print("OPENAI_REASONING_EFFORT: (not set)")
    print()

    if args.print_facts:
        print("-- facts --")
        print(json.dumps(facts, indent=2, ensure_ascii=False))
        print()

    print("-- fallback (template) --")
    print(fallback)
    print()

    if not enabled:
        print("AI rewrite is disabled. Re-run with --ai or set AI_REWRITE_ENABLED=1.")
        return 0

    sys_prompt, user_prompt, extra_fields = _get_prompts(args.kind, facts, fallback)

    if resolved_mode == "responses":
        body = _build_responses_body(
            model=model,
            sys_prompt=sys_prompt,
            user_prompt=user_prompt,
            extra_fields=extra_fields,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort or None,
            include_temperature=supports_temp,
        )
    else:
        body = _build_chat_body(
            model=model,
            sys_prompt=sys_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort or None,
            include_temperature=supports_temp,
        )

    if args.print_request:
        print("-- request payload --")
        print(json.dumps(body, indent=2, ensure_ascii=False))
        print()

    t0 = time.time()
    content, err, status = _call_openai(endpoint_path=endpoint_path, body=body, print_response=args.print_response)
    dt = time.time() - t0

    if err:
        print("-- rewrite (error) --")
        print(err)
        if status is not None:
            print(f"HTTP status: {status}")
        print(f"Elapsed: {dt:.2f}s")
        return 1


    raw_text = content or ""
    collapsed = _collapse_blank_lines(raw_text)

    if args.show_raw:
        print("-- rewrite (raw) --")
        print(raw_text)

    print("-- rewrite (OpenAI) --")
    print(collapsed)
    print(f"\nElapsed: {dt:.2f}s")

    if args.validate:
        expected = _expected_uids_from_facts(facts)
        diag = _analyze_output_text(raw_text, expected)
        _print_diagnostics(diag)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
