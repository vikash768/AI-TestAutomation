"""QA test data a native run creates, and the proof that it owns it.

Every value a journey types is generated here and carries the run's marker
(qa_<run_id>_<HHMMSS>), so the run can recognise its own data on screen later.
The registry is the only list of things a run may change or delete afterwards:

  * a control that changes or deletes data is allowed only inside a row or a
    screen that shows one of this run's values (row_owner / owned_screen)
  * everything recorded here is cleaned up at the end (also from `finally`
    after a failure); what cannot be cleaned is reported as a leftover
  * the ledger is written to qa_data.json after every change, so a run that
    dies halfway still leaves the list behind
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from qaplatform.app.device import Snapshot, _bounds
from qaplatform.explore.crawler import ObservedElement


class QADataRegistry:
    def __init__(self, run_id: str, path: Path | None = None):
        self.run_id = run_id
        self.stamp = time.strftime("%H%M%S")
        self.path = path
        self.records: list[dict] = []
        self.marker = re.compile(rf"qa[_-]?{re.escape(run_id[:6])}", re.I)

    # -- values -------------------------------------------------------------
    def value(self, kind: str = "qa", max_len: int | None = None, style: int = 0) -> str:
        """qa_<run>_<time> (style 0), QA-<run>-<time> (style 1: letters, digits, dashes), QA<run6><time> (style 2: letters and digits)."""
        base = [f"qa_{self.run_id}_{self.stamp}", f"QA-{self.run_id}-{self.stamp}", f"QA{self.run_id[:6]}{self.stamp}"][min(style, 2)]
        if kind == "qa_update":
            base += ["_upd", "-UPD", "U"][min(style, 2)]
        elif kind == "qa_note":
            base = f"QA test note {base}"
        if max_len and len(base) > max_len:
            short = ["qa_", "QA-", "QA"][min(style, 2)] + self.run_id[:6]
            base = (short + self.stamp)[:max_len] if max_len >= len(short) + 2 else short[:max_len]
        return base

    def resolve(self, template: str, max_len: int | None = None, style: int = 0) -> str:
        """Fill a step's value: {qa}, {qa_update}, {qa_note}, {empty}, {long:N}; anything else is a literal test value."""
        t = (template or "").strip()
        if t in ("{qa}", "{qa_update}", "{qa_note}"):
            return self.value(t[1:-1], max_len, style)
        if t == "{empty}":
            return ""
        if m := re.fullmatch(r"\{long:(\d{1,4})\}", t):
            n = min(int(m.group(1)), 1000)
            seed = self.value("qa", None, 2)
            return (seed * (n // len(seed) + 1))[:n]
        if m := re.fullmatch(r"\{number(?::(-?\d+))?\}", t):
            return m.group(1) or "1"
        return template or ""

    def is_ours(self, text: str | None) -> bool:
        return bool(text) and bool(self.marker.search(text))

    # -- ledger -------------------------------------------------------------
    def add(self, value: str, entity: str, where: str, journey: str, kind: str = "record", original: str | None = None, **extra) -> dict:
        for r in self.records:
            if r["id"] == value and r["where"] == where and r["kind"] == kind:
                return r
        rec = {"id": value, "entity": entity, "where": where, "journey": journey, "kind": kind, "original": original, "status": "created", "note": "", **extra}
        self.records.append(rec)
        self.save()
        return rec

    def set_status(self, rec: dict, status: str, note: str = "") -> None:
        rec["status"], rec["note"] = status, note or rec.get("note", "")
        self.save()

    def pending(self) -> list[dict]:
        return [r for r in self.records if r["status"] not in ("cleaned", "not_retained")]

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.write_text(json.dumps({"run_id": self.run_id, "records": self.public()}, indent=1), encoding="utf-8")
        except Exception:
            pass

    def public(self) -> list[dict]:
        # the original value of a field can be the user's own data: keep only whether there was one
        return [{k: (bool(v) if k == "original" else v) for k, v in r.items()} for r in self.records]

    def summary(self) -> dict:
        cleaned = sum(1 for r in self.records if r["status"] in ("cleaned", "not_retained"))
        return {"created": len(self.records), "cleaned": cleaned, "leftover": len(self.records) - cleaned, "records": self.public()}


# -- ownership on screen --------------------------------------------------------
def _text_boxes(snap: Snapshot) -> list[tuple[str, list[int], list[int] | None]]:
    """(text, its box, the box of the row it sits in) for every text node in the UI tree."""
    if not snap.xml:
        return []
    try:
        root = ET.fromstring(snap.xml)
    except ET.ParseError:
        return []
    parent = {c: p for p in root.iter() for c in p}
    width = snap.width or int(root.get("width") or 0)
    out = []
    for node in root.iter():
        text = (node.get("text") or node.get("content-desc") or node.get("label") or node.get("value") or "").strip()
        if not text:
            continue
        box = _bounds(node.get("bounds", "")) or _xywh(node)
        row = None
        cur = node
        while cur in parent:
            cur = parent[cur]
            b = _bounds(cur.get("bounds", "")) or _xywh(cur)
            if len(b) == 4 and (cur.get("clickable") == "true" or (width and b[2] >= 0.6 * width and snap.height and b[3] <= 0.35 * snap.height)):
                row = b
                break
        out.append((text, box, row))
    return out


def _xywh(node) -> list[int]:
    try:
        return [int(float(node.get(k))) for k in ("x", "y", "width", "height")]
    except (TypeError, ValueError):
        return []


def _inside(point: tuple[float, float], box: list[int] | None) -> bool:
    return bool(box) and len(box) == 4 and box[0] <= point[0] <= box[0] + box[2] and box[1] <= point[1] <= box[1] + box[3]


def row_owner(snap: Snapshot, el: ObservedElement, registry: QADataRegistry) -> str | None:
    """The QA value of the row this control belongs to, or None when the row shows no data of this run."""
    if registry.is_ours(el.name):
        return el.name
    if len(el.bbox) != 4:
        return None
    centre = (el.bbox[0] + el.bbox[2] / 2, el.bbox[1] + el.bbox[3] / 2)
    for text, box, row in _text_boxes(snap):
        if registry.is_ours(text) and _inside(centre, row):
            return text
    return None


def owned_screen(snap: Snapshot, registry: QADataRegistry) -> bool:
    """A detail screen of this run's record: it shows our value, and no list of other records."""
    ours = [t for t in snap.texts if registry.is_ours(t)] + [e.name for e in snap.elements if registry.is_ours(e.name)]
    if not ours:
        return False
    our_rows = [e for e in snap.elements if e.visible and registry.is_ours(e.name) and len(e.bbox) == 4]
    for e in snap.elements:
        if not e.visible or registry.is_ours(e.name) or len(e.bbox) != 4:
            continue
        if len(e.name or "") > 25 and e.role == "button":
            return False  # list rows announce their contents
        for r in our_rows:  # another control shaped like our row: this is a list of records, not ours alone
            if e.tag == r.tag and abs(e.bbox[2] - r.bbox[2]) <= 0.05 * max(1, r.bbox[2]) and abs(e.bbox[3] - r.bbox[3]) <= 0.2 * max(1, r.bbox[3]):
                return False
    return True
