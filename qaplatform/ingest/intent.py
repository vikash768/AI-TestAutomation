"""Turn parsed code facts into an intent spec per element.

The deterministic part reads the element's own attributes and the handler
facts the parser found. The optional LLM part asks Gemini to summarise a page
file into one-line expectations per element plus page-level persistence and
lifecycle rules. The LLM never sees credentials: it reads source code only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from qaplatform.explore.safety import classify_action
from qaplatform.ingest.js_parser import ApiCall, CodeElement, CodeInventory

logger = logging.getLogger(__name__)


def _api_index(inv: CodeInventory) -> dict[str, ApiCall]:
    index: dict[str, ApiCall] = {}
    for call in inv.api_calls:
        if call.function:
            index.setdefault(call.function, call)
    return index


def build_intent(el: CodeElement, inv: CodeInventory, api_index: dict[str, ApiCall] | None = None) -> dict:
    api_index = api_index if api_index is not None else _api_index(inv)
    expected: list[str] = []
    api_calls: list[dict] = []
    for name in el.calls:
        short = name.split(".")[-1]
        hit = api_index.get(short)
        if hit:
            api_calls.append({"function": short, "method": hit.method, "path": hit.path})
    # calls made later from content this control opens (a drawer form's onSave)
    deferred_api_calls: list[dict] = []
    for name in el.deferred_calls:
        short = name.split(".")[-1]
        hit = api_index.get(short)
        if hit and not any(c["function"] == short for c in api_calls):
            deferred_api_calls.append({"function": short, "method": hit.method, "path": hit.path})
    session_ending = "logout" in el.calls or (el.handler or "").lower().find("logout") >= 0
    submits_form = el.kind == "button" and (el.attrs.get("type") == "submit") and el.in_form

    if el.kind == "link" and el.href:
        if el.href.startswith("/"):
            expected.append(f"Navigates to {el.href} within the app")
        elif el.href.startswith("http"):
            expected.append(f"Opens external link {el.href}")
        else:
            expected.append(f"Navigates to {el.href}")
    for target in el.navigates_to:
        if target != el.href:
            expected.append(f"Navigates to {target}")
    if "drawer" in el.opens:
        expected.append("Opens a side drawer; nothing is saved until the drawer's form is submitted")
    if "confirm" in el.opens:
        expected.append("Opens a confirmation dialog; nothing changes until confirmed")
    if "toggle" in el.opens:
        expected.append("Toggles a menu or disclosure open/closed")
    if submits_form:
        expected.append(f"Submits the enclosing form ({el.form_handler or 'inline handler'})")
    if session_ending:
        expected.append("Ends the session and returns to /login; protected routes must then redirect to /login")
    for c in api_calls:
        expected.append(f"Calls {c['method']} {c['path']}")
    if el.kind in ("input", "textarea", "select"):
        t = el.attrs.get("type", "text")
        if el.attrs.get("required") is True:
            expected.append("Required: the form cannot submit while empty")
        if t == "email":
            expected.append("Browser rejects values that are not an email address")
        if t == "number":
            expected.append("Accepts numeric input only" + (f", min {el.attrs['min']}" if "min" in el.attrs else "") + (f", max {el.attrs['max']}" if "max" in el.attrs else ""))
        if t == "password":
            expected.append("Masks input")
        for k in ("minLength", "maxLength", "pattern"):
            if k in el.attrs:
                expected.append(f"{k} = {el.attrs[k]}")
    if el.kind in ("switch", "checkbox", "radio"):
        expected.append("Changes its checked state when clicked")
    if el.kind == "tab":
        expected.append("Shows its panel and becomes the active tab")
    if isinstance(el.attrs.get("disabled"), str) and el.attrs.get("disabled") not in ("True", "False"):
        expected.append(f"Disabled while `{el.attrs['disabled']}`")
    if not expected:
        if el.handler:
            expected.append(f"Runs handler {el.handler}")
        else:
            expected.append("Responds to click without errors")

    intent = {
        "expected": expected,
        "handler": el.handler or "",
        "navigates_to": el.navigates_to,
        "opens": el.opens,
        "api_calls": api_calls,
        "deferred_api_calls": deferred_api_calls,
        "submits_form": submits_form,
        "session_ending": session_ending,
        "conditional": el.conditional,
        "repeated": el.repeated,
        "shared": el.shared,
    }
    cls = classify_action(el.label, {**el.attrs, "href": el.href}, intent)
    if cls == "mutating" and el.kind == "link" and not api_calls and not submits_form:
        cls = "safe"
    if cls == "mutating" and el.kind in ("input", "textarea", "select", "checkbox", "radio", "switch", "tab") and not api_calls:
        cls = "safe"
    intent["action_class"] = cls
    return intent


# ---------------------------------------------------------------------------
# optional LLM enrichment
# ---------------------------------------------------------------------------

ENRICH_PROMPT = """You are reading one source file of a web application's frontend so a test platform can learn what the UI is supposed to do.
Return ONLY a JSON object with this shape:
{
  "page_summary": "one sentence",
  "persistence_rules": ["what must survive a reload / navigation, what must not"],
  "lifecycle_rules": ["timers, polling intervals, auto-logout, session expiry, anything that happens after time passes"],
  "must_never": ["things that must never happen on this page"],
  "elements": [{"label": "<label as it appears to the user>", "expected": "one line: what happens when the user uses it"}]
}
Only describe behaviour that is visible in the code. Do not invent features.

FILE: {file}
---
{source}
"""


def _strip_secrets(text: str) -> str:
    return re.sub(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]+['\"]", r"\1: '<redacted>'", text)


def _generate_json(client, types, models: list[str], prompt: str, pause_s: float) -> tuple[dict | None, str]:
    """Call the first model that answers; back off on 429/503; return (data, error)."""
    import time as _time
    last = ""
    for model in models:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
                )
                data = json.loads(resp.text or "{}")
                _time.sleep(pause_s)
                return (data if isinstance(data, dict) else None), ""
            except Exception as exc:
                last = f"{model}: {str(exc)[:160]}"
                text = str(exc)
                if "429" in text or "RESOURCE_EXHAUSTED" in text or "503" in text or "UNAVAILABLE" in text:
                    _time.sleep(min(2 ** attempt * 3, 20))
                    continue
                break  # a non-quota error: try the next model, not the same one again
    return None, last


def enrich_with_llm(inv: CodeInventory, repo_root: Path, cache_dir: Path, api_key: str, model: str, max_files: int = 40, fallback_model: str = "gemini-3.5-flash-lite", pause_s: float = 1.5) -> dict[str, dict]:
    """Return {file: enrichment} for page and layout files. Failures are logged, never raised."""
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover
        logger.warning("google-genai unavailable: %s", exc)
        return {}
    client = genai.Client(api_key=api_key)
    cache_dir.mkdir(parents=True, exist_ok=True)
    targets = sorted({e.file for e in inv.elements})[:max_files]
    out: dict[str, dict] = {}
    models = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])
    failures: list[str] = []
    for rel in targets:
        path = repo_root / rel
        if not path.exists():
            continue
        source = _strip_secrets(path.read_text(encoding="utf-8", errors="replace"))
        digest = hashlib.sha256((model + rel + source).encode()).hexdigest()
        cached = cache_dir / f"{digest}.json"
        if cached.exists():
            try:
                out[rel] = json.loads(cached.read_text())
                continue
            except Exception:
                pass
        prompt = ENRICH_PROMPT.replace("{file}", rel).replace("{source}", source[:60000])
        data, err = _generate_json(client, types, models, prompt, pause_s)
        if data is not None:
            out[rel] = data
            cached.write_text(json.dumps(data))
        else:
            failures.append(f"{rel}: {err}")
    if failures:
        logger.warning("LLM enrichment failed for %d/%d files; first: %s", len(failures), len(targets), failures[0])
        out["__failures__"] = {"count": len(failures), "first": failures[:3]}
    return out


def apply_enrichment(elements: list[CodeElement], intents: dict[str, dict], enrichment: dict[str, dict]) -> None:
    """Attach LLM one-liners to matching elements by label, and page rules to all elements of the file."""
    def norm(s: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    for el in elements:
        data = enrichment.get(el.file)
        if not data or el.file == "__failures__":
            continue
        intent = intents.get(el.key)
        if intent is None:
            continue
        intent.setdefault("page_rules", {
            "summary": data.get("page_summary", ""),
            "persistence": data.get("persistence_rules", []),
            "lifecycle": data.get("lifecycle_rules", []),
            "must_never": data.get("must_never", []),
        })
        target = norm(el.label)
        if not target:
            continue
        for item in data.get("elements", []) or []:
            if isinstance(item, dict) and norm(item.get("label")) == target and item.get("expected"):
                intent["llm_expected"] = str(item["expected"])[:300]
                break
