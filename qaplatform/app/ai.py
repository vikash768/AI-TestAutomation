"""The LLM's part in a native run: intent, journeys and a second look at results.

Inputs: the code inventory (screens, controls, the code's own rules), short
excerpts of screen files that hold forms, the backend's routes, and what the
explorer saw on the device (screen titles, control names, visible texts).

  plan()    screen intent, expected behaviour per control, user journeys
            (create / verify / update / delete / validation / relaunch) and
            validation cases, in the step format of app/journeys.py
  review()  reads each journey's step log and flags results that look wrong
            for the screen's intent; these become needs_review findings only

Never sent: the test account's username or password, Git tokens, secrets
found in the code (redacted), anything typed into password fields. The LLM
never acts: its journeys go through journeys.validate() and the device-side
ownership checks like any other plan. Without GEMINI_API_KEY, or when the call
fails, plan() returns None and the run uses the plan derived from the code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from qaplatform.app.backend import BackendRoute, ClientEndpoint
from qaplatform.app.explore import Screen
from qaplatform.config import Settings
from qaplatform.ingest.intent import _generate_json, _strip_secrets
from qaplatform.ingest.js_parser import CodeInventory

logger = logging.getLogger(__name__)

PLAN_PROMPT = """You are a senior QA engineer planning end-to-end tests for a native mobile app.
You get the app's screens and controls (from its source code), short source excerpts of screens with forms,
what a test robot actually saw on each screen of the running app, and the backend API routes.

Return ONLY a JSON object:
{
  "screens": [{"route": "<screen name from the list>", "intent": "one sentence: what the user does here"}],
  "controls": [{"route": "<screen>", "label": "<control label from the list>", "expected": "one line: what must happen when it is used"}],
  "journeys": [{
     "name": "short name", "kind": "create|update|delete|validation|flow", "rationale": "why this matters",
     "steps": [{"action": "open|type|tap|submit|verify_text|verify_absent|relaunch|back",
                "screen": "<screen name the step happens on>", "target": "<exact control label from the lists>",
                "value": "{qa} | {qa_update} | {qa_note} | {empty} | {long:N} | {number} | a short literal for an invalid case",
                "expect": "navigates:<screen> | rejected | accepted | maxlen:<n> | <empty>",
                "effect": "none|navigation|local_draft|local_record|remote", "why": "the line of code or text that supports this"}]
  }],
  "api_ui": [{"route": "<screen>", "control": "<label>", "endpoint": "METHOD /path"}]
}
Rules:
- Use only screen names and control labels that appear in the lists below. Do not invent features.
- The first step of every journey is "open" on the screen where it starts.
- Values: "{qa}" is this run's QA identifier and must be used for any name / id the journey creates, so the run can find and delete its own data;
  "{qa_update}" for an edited value; literals only for invalid or boundary inputs; never real personal data.
- Cover: creating data with the QA identifier and verifying it is shown (verify_text), persistence after "relaunch",
  editing it ({qa_update}) and deleting it (only data the same journey created), plus validation cases (positive, negative, boundary)
  for each field whose rules the code shows (required, length, format).
- effect: "remote" whenever the step (or the screen it opens) sends or later uploads data to the server.
- Never plan: signing out, deleting or editing data the journey did not create, "sync all" / "upload all" / "clear" style bulk actions,
  payments, permission or system settings, sharing, leaving the app. Steps that need physical hardware: plan them, the robot will block them.
- At most 8 journeys, at most 15 steps each.

SCREENS AND CONTROLS (from the code):
{inventory}

SOURCE EXCERPTS (screens with forms):
{sources}

SEEN ON THE DEVICE:
{observed}

BACKEND API:
{backend}
"""

REVIEW_PROMPT = """You review the results of end-to-end tests a robot ran on a native mobile app.
For each journey you get its intent, the steps with what the robot observed, and its status.
Return ONLY JSON: {"reviews": [{"name": "<journey name>", "verdict": "ok|suspicious", "reason": "one sentence citing the observed text"}]}
Mark "suspicious" only when an observation shows the app doing something wrong for a user (a wrong value, a confusing or missing message,
data not kept), not when the robot was blocked by its own safety rules or by missing hardware.

JOURNEYS:
{journeys}
"""


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    out = text or ""
    for s in secrets:
        if s and len(s) >= 3:
            out = re.sub(re.escape(s), "<user>", out, flags=re.I)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", out)


def _inventory_text(inv: CodeInventory) -> str:
    lines = []
    for r in inv.routes:
        els = [e for e in inv.elements if e.route == r.path and (e.label or e.kind == "input")]
        lines.append(f"- {r.path} ({inv.page_files.get(r.path, r.file)}){' [sign-in required]' if r.protected else ''}")
        for e in els[:25]:
            rules = {k: e.attrs[k] for k in ("maxLength", "required", "keyboardType", "secure", "readOnly") if k in e.attrs and e.attrs[k] not in (None, False, "")}
            bits = [f"{e.kind} '{e.label}'"]
            if e.navigates_to:
                bits.append("-> " + ", ".join(e.navigates_to))
            if e.opens:
                bits.append("opens " + "/".join(e.opens))
            if e.attrs.get("writes"):
                bits.append("sends " + "/".join(e.attrs["writes"]))
            if rules:
                bits.append(json.dumps(rules))
            lines.append("    " + " ".join(bits))
    return "\n".join(lines)[:14000]


def _sources_text(inv: CodeInventory, repo: Path | None) -> str:
    if repo is None:
        return "(not available)"
    out, size = [], 0
    for r in inv.routes:
        if not any(e.route == r.path and e.kind == "input" for e in inv.elements):
            continue
        f = inv.page_files.get(r.path)
        if not f or not (repo / f).is_file() or re.search(r"login|sign|auth|pin|lock|password", r.path, re.I):
            continue  # sign-in screens: the account's handling is not the LLM's business
        src = _strip_secrets((repo / f).read_text(encoding="utf-8", errors="replace"))
        src = re.sub(r"\n\s*\n+", "\n", re.split(r"\nconst useStyles|\nconst styles\s*=|StyleSheet\.create", src)[0])[:5000]
        out.append(f"--- {f} ({r.path})\n{src}")
        size += len(src)
        if size > 20000:
            break
    return "\n".join(out) or "(no form screens)"


def _observed_text(screens: list[Screen], secrets: tuple[str, ...]) -> str:
    lines = []
    for s in screens[:30]:
        if s.kind == "menu":
            continue
        names = [e.name for e in s.snap.elements if e.visible and e.name and e.type != "password"][:25]
        texts = [t for t in s.snap.texts if len(t) < 120][:25]
        lines.append(_scrub(f"- {s.route or s.snap.title} (reached by: {' > '.join(e.name for e in s.path) or 'launch'}): controls {names}; texts {texts}", secrets))
    return "\n".join(lines)[:12000]


def _backend_text(routes: list[BackendRoute], client: list[ClientEndpoint]) -> str:
    lines = [f"{r.method} {r.path}{' [auth]' if r.auth else ''} ({r.file}:{r.line})" for r in routes[:80]]
    lines += [f"app calls {e.method} {e.path} ({e.file}:{e.line})" for e in client[:40]]
    return "\n".join(lines) or "(no backend code given)"


def _client(settings: Settings):
    from google import genai
    from google.genai import types
    return genai.Client(api_key=settings.gemini_api_key), types


def _cached(settings: Settings, kind: str, prompt: str) -> tuple[dict | None, str]:
    cache = settings.artifacts_dir / "cache" / "native_ai"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((settings.gemini_model + kind + prompt).encode()).hexdigest()
    hit = cache / f"{key}.json"
    if hit.exists():
        try:
            return json.loads(hit.read_text()), ""
        except Exception:
            pass
    try:
        client, types = _client(settings)
    except Exception as exc:
        return None, f"google-genai unavailable: {exc}"
    models = [settings.gemini_model] + ([settings.gemini_fallback_model] if settings.gemini_fallback_model and settings.gemini_fallback_model != settings.gemini_model else [])
    data, err = _generate_json(client, types, models, prompt, 0.5)
    if data is not None:
        hit.write_text(json.dumps(data))
    return data, err


def enabled(settings: Settings, options: dict) -> bool:
    return bool(settings.gemini_api_key) and settings.llm_intent and bool(options.get("llm_intent", True))


def plan(settings: Settings, inv: CodeInventory, screens: list[Screen], repo: Path | None, routes: list[BackendRoute], client: list[ClientEndpoint],
         secrets: tuple[str, ...]) -> tuple[dict | None, str]:
    """(plan, error). The prompt holds no credentials: the account's username and password are scrubbed from everything seen on the device."""
    prompt = (PLAN_PROMPT.replace("{inventory}", _inventory_text(inv)).replace("{sources}", _sources_text(inv, repo))
              .replace("{observed}", _observed_text(screens, secrets)).replace("{backend}", _backend_text(routes, client)))
    if any(s and len(s) >= 3 and s in prompt for s in secrets):
        return None, "refused: a credential would have been sent"
    return _cached(settings, "plan", prompt)


def apply_intent(plan_data: dict | None, inv: CodeInventory, intents: dict) -> int:
    """Attach the LLM's per-control expectation (llm_expected) and screen intent (page_rules.summary), as web runs do."""
    if not plan_data:
        return 0
    screen_intent = {str(s.get("route")): str(s.get("intent"))[:300] for s in plan_data.get("screens", []) if isinstance(s, dict)}
    expected = {(str(c.get("route")), re.sub(r"\W+", " ", str(c.get("label") or "").lower()).strip()): str(c.get("expected"))[:300]
                for c in plan_data.get("controls", []) if isinstance(c, dict) and c.get("expected")}
    n = 0
    for el in inv.elements:
        intent = intents.get(el.key)
        if intent is None:
            continue
        if el.route in screen_intent:
            intent.setdefault("page_rules", {"summary": screen_intent[el.route]})
        hit = expected.get((el.route or "", re.sub(r"\W+", " ", (el.label or "").lower()).strip()))
        if hit:
            intent["llm_expected"] = hit
            n += 1
    return n


def review(settings: Settings, results: list, secrets: tuple[str, ...]) -> tuple[list[dict], str]:
    rows = []
    for r in results:
        if r.status == "blocked" and not r.log:
            continue
        rows.append(_scrub(json.dumps({"name": r.journey.name, "intent": r.journey.rationale, "status": r.status, "reason": r.reason,
                                       "steps": [{k: v for k, v in s.items() if k in ("action", "target", "value", "expect", "result")} for s in r.log]})[:3000], secrets))
    if not rows:
        return [], ""
    data, err = _cached(settings, "review", REVIEW_PROMPT.replace("{journeys}", "\n".join(rows)[:20000]))
    return [x for x in (data or {}).get("reviews", []) if isinstance(x, dict)], err
