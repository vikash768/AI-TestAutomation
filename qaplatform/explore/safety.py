"""Decide what the platform may click on its own.

Everything is non-destructive by default. A control is classified from the
code-derived intent when there is one, otherwise from its label and
attributes. The classes are:

  safe            navigation, disclosure, filters, toggles: click freely
  mutating        creates or changes data: skipped unless the run allows it
  destructive     deletes, deactivates, resets, purges: never clicked
  session_ending  logs out: run last, once
  download        points at a file: verified by request, not clicked
"""
from __future__ import annotations

import re

DESTRUCTIVE = re.compile(
    r"\b(delete|remove|purge|clean|wipe|erase|destroy|deactivate|revoke|reset|archive|unassign|danger|clear|permanently|terminate|cancel subscription)\b",
    re.I,
)
SESSION_ENDING = re.compile(r"\b(log ?out|sign ?out|logout)\b", re.I)
MUTATING = re.compile(
    r"\b(save|submit|create|update|send|apply|confirm|generate|invite|restore|reactivate|activate|start|stop|pause|resume|upload|import|publish|approve|reject|assign|change password|rename|move|mark)\b",
    re.I,
)
DOWNLOAD_EXT = re.compile(r"\.(dmg|exe|zip|pdf|csv|xlsx|msi|pkg|tar\.gz|apk)(\?|$)", re.I)
NON_GET = {"POST", "PUT", "PATCH", "DELETE"}


def classify_action(label: str | None, attrs: dict | None = None, intent: dict | None = None) -> str:
    attrs = attrs or {}
    intent = intent or {}
    text = " ".join(filter(None, [label or "", attrs.get("aria-label") or "", attrs.get("title") or ""]))
    href = attrs.get("href") or attrs.get("to") or ""

    if intent.get("session_ending") or SESSION_ENDING.search(text) or SESSION_ENDING.search(str(intent.get("handler", ""))):
        return "session_ending"
    if href and DOWNLOAD_EXT.search(href) or attrs.get("download") is not None:
        return "download"

    api_calls = intent.get("api_calls") or []
    methods = {str(c.get("method", "")).upper() for c in api_calls if isinstance(c, dict)}
    if "DELETE" in methods:
        return "destructive"
    if intent.get("destructive"):
        return "destructive"
    if DESTRUCTIVE.search(text):
        return "destructive"

    # Opening a drawer, menu or confirm dialog changes nothing by itself.
    if intent.get("opens") and not (methods & NON_GET):
        return "safe"
    if methods & NON_GET:
        return "mutating"
    if attrs.get("type") == "submit" or intent.get("submits_form"):
        return "mutating"
    if MUTATING.search(text):
        return "mutating"
    return "safe"


def is_clickable_in_smoke(action_class: str, allow_mutations: bool = False) -> bool:
    if action_class == "safe":
        return True
    if action_class == "mutating":
        return allow_mutations
    return False
