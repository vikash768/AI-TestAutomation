"""Match code-declared elements to elements observed on the live app."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from qaplatform.explore.crawler import ObservedElement, ObservedPage
from qaplatform.ingest.js_parser import CodeElement

INTERACTIVE_KINDS = {"button", "link", "input", "select", "textarea", "checkbox", "radio", "switch", "tab", "menuitem", "clickable", "file"}
GROUP_OF_KIND = {
    "button": "button", "clickable": "button", "switch": "button", "tab": "button", "menuitem": "button", "checkbox": "button", "radio": "button",
    "link": "link", "input": "input", "textarea": "input", "file": "input", "select": "select",
}
GROUP_OF_ROLE = {
    "button": "button", "switch": "button", "tab": "button", "menuitem": "button", "menuitemcheckbox": "button", "checkbox": "button", "radio": "button",
    "link": "link", "textbox": "input", "searchbox": "input", "spinbutton": "input", "slider": "input", "file": "input",
    "combobox": "select", "listbox": "select",
}


def label_pattern(label: str | None) -> re.Pattern | None:
    """'Download for {label}' -> ^download for .+$ ; needs at least 3 literal characters."""
    if not label or ("{" not in label and "${" not in label):
        return None
    parts = re.split(r"\$?\{[^}]*\}", label)
    literal = "".join(parts)
    if len(normalize(literal)) < 3:
        return None
    rx = ".+".join(re.escape(normalize(p)).replace("\\ ", " ") for p in parts if p is not None)
    rx = re.sub(r"(\.\+)(\s*\.\+)+", ".+", rx).strip()
    return re.compile("^" + rx.strip() + "$")


def normalize(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"[…→←▾▼✕×✓]+", " ", s)
    s = s.replace("...", " ")
    s = re.sub(r"[^a-z0-9&/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Match:
    code: CodeElement
    page: ObservedPage | None
    observed: ObservedElement | None
    score: int = 0
    reason: str = ""


@dataclass
class ReconcileResult:
    matches: list[Match] = field(default_factory=list)
    unmatched_code: list[CodeElement] = field(default_factory=list)
    unmatched_observed: list[tuple[ObservedPage, ObservedElement]] = field(default_factory=list)
    coverage: float = 0.0
    coverage_unconditional: float = 0.0
    stats: dict = field(default_factory=dict)


def _score(code: CodeElement, obs: ObservedElement, origin: str) -> tuple[int, str]:
    cg = GROUP_OF_KIND.get(code.kind, "button")
    og = GROUP_OF_ROLE.get(obs.role, "button" if obs.tag in ("button", "a", "summary") else None)
    if og is None and obs.tag == "a":
        og = "link"
    score = 0
    reasons = []
    if og == cg:
        score += 2
        reasons.append("kind")
    elif cg == "button" and og == "link" or cg == "link" and og == "button":
        score += 1
    elif code.kind == "clickable" and og == "select":
        score += 1  # a custom component (<Dropdown label="Status">) rendering a combobox
    else:
        return 0, ""
    # Literal attributes that contradict the live element rule a pairing out, however
    # close the rest looks (a template label like {initials} otherwise matches on kind alone).
    ctype, otype = code.attrs.get("type"), (obs.type or "").lower()
    if code.kind == "button" and ctype in ("button", "submit", "reset") and otype in ("button", "submit", "reset") and ctype != otype:
        return 0, ""
    popup = str(code.attrs.get("aria-haspopup") or "").strip("{}\"' ").lower()
    if popup in ("menu", "listbox", "dialog", "tree", "grid", "true") and not obs.haspopup and obs.tag == "button":
        return 0, ""
    cl = normalize(code.label)
    on = normalize(obs.name)
    alts = [normalize(a) for a in code.label_alternatives if a]
    pat = label_pattern(code.label)
    if pat is not None and on and pat.match(on):
        score += 4
        reasons.append("label~tpl")
    elif cl and on:
        if cl == on or on in alts:
            score += 4
            reasons.append("label=")
        elif len(cl) >= 8 and on.startswith(cl):
            score += 3
            reasons.append("label^")
        elif cl in on or on in cl:
            score += 2
            reasons.append("label~")
        elif any(a and (a in on or on in a) for a in alts):
            score += 2
            reasons.append("alt~")
    if code.href and obs.href:
        cpath = code.href if code.href.startswith("/") else urlparse(code.href).path
        opath = urlparse(obs.href).path
        if cpath and (opath == cpath or opath.rstrip("/") == cpath.rstrip("/")):
            score += 4
            reasons.append("href=")
    # Structural evidence for controls whose label is a template ({initials}): the same popup
    # kind and the same explicit button type.
    templated = "{" in (code.label or "") and label_pattern(code.label) is None and len(normalize(obs.name)) <= 4
    if templated and popup and obs.haspopup and popup == str(obs.haspopup).lower():
        score += 2
        reasons.append("haspopup=")
    if templated and code.kind == "button" and ctype in ("button", "submit", "reset") and ctype == otype:
        score += 1
    ph = code.attrs.get("placeholder")
    if isinstance(ph, str) and ph and obs.placeholder and normalize(ph) == normalize(obs.placeholder):
        score += 4
        reasons.append("placeholder=")
    if isinstance(code.attrs.get("id"), str) and obs.id and code.attrs["id"] == obs.id:
        score += 4
        reasons.append("id=")
    if code.kind == "input" and obs.role in ("textbox", "searchbox", "spinbutton") and code.attrs.get("type") == obs.type:
        score += 1
    if code.kind == "input" and cl and on and cl == on:
        score += 1
    return score, "+".join(reasons)


def reconcile(code_elements: list[CodeElement], pages: list[ObservedPage], protected_routes: list[str], origin: str) -> ReconcileResult:
    res = ReconcileResult()
    by_route: dict[str, list[ObservedPage]] = {}
    for p in pages:
        by_route.setdefault(p.route, []).append(p)
    protected_pages = [p for p in pages if p.route in protected_routes or p.protected]
    used: set[tuple[int, int]] = set()  # (id(page), index of element)
    # The sign-in screen: declares a password field AND a sign-in button. (A user-management
    # screen also has password fields — create user, reset password — but no sign-in button.)
    pw_routes = {e.route for e in code_elements if str((e.attrs or {}).get("type") or "").lower() == "password"}
    signin_routes = {e.route for e in code_elements if re.fullmatch(r"(sign ?in|log ?in)", normalize(e.label))}
    login_routes = pw_routes & signin_routes

    for code in code_elements:
        if code.kind not in INTERACTIVE_KINDS:
            continue
        # A pseudo-page (#Screen from a router-less SPA) or the shared layout is matched
        # against every authenticated page, since the live app shows them all on one URL.
        if code.route == "(layout)":
            candidates: list[ObservedPage] = protected_pages or pages
        elif (code.route or "").startswith("#"):
            # Only the sign-in screen's code (it declares a password field) may match the public
            # sign-in page; every other screen is matched against signed-in pages only.
            public = [p for p in pages if p not in protected_pages]
            candidates = (public or pages) if code.route in login_routes else (protected_pages or pages)
        else:
            candidates = by_route.get(code.route, [])
        best: Match | None = None
        for page in candidates:
            for idx, obs in enumerate(page.elements):
                score, reason = _score(code, obs, origin)
                if score > 0 and getattr(page, "revealed", False):
                    # Inside something a click opened: co-location with the opener and
                    # data-driven labels ({cell.day}) are real evidence, not noise.
                    if getattr(page, "opener_file", None) and code.file == page.opener_file:
                        score += 2
                        reason += "+file"
                    if not normalize(re.sub(r"\$?\{[^}]*\}", " ", code.label or "")):
                        score += 2
                        reason += "+wild"
                if score < 5:
                    continue
                if not obs.visible:
                    score -= 1
                key = (id(page), idx)
                if key in used and not code.repeated:
                    score -= 2
                if best is None or score > best.score:
                    best = Match(code=code, page=page, observed=obs, score=score, reason=reason)
        if best is not None and best.score >= 5:
            res.matches.append(best)
            used.add((id(best.page), best.page.elements.index(best.observed)))
            # A layout control appears on every page and a list control on every row:
            # every observed twin counts as declared, not as an undocumented control.
            if code.route == "(layout)" or code.repeated or code.shared:
                for page in candidates:
                    for idx, obs in enumerate(page.elements):
                        if obs is best.observed:
                            continue
                        s, _ = _score(code, obs, origin)
                        if s >= 5 and (code.repeated or label_pattern(code.label) is not None or normalize(obs.name) == normalize(best.observed.name)):
                            res.matches.append(Match(code=code, page=page, observed=obs, score=s, reason="twin"))
        else:
            res.unmatched_code.append(code)
            res.matches.append(Match(code=code, page=None, observed=None, score=0, reason="not found"))

    matched_keys = {(id(m.page), id(m.observed)) for m in res.matches if m.observed is not None}
    for page in pages:
        for obs in page.elements:
            if (id(page), id(obs)) in matched_keys:
                continue
            if not obs.visible:
                continue
            res.unmatched_observed.append((page, obs))

    interactive = [c for c in code_elements if c.kind in INTERACTIVE_KINDS]
    found = [m for m in res.matches if m.observed is not None and m.reason != "twin"]
    uncond = [c for c in interactive if not c.conditional and not c.repeated]
    found_uncond = [m for m in found if not m.code.conditional and not m.code.repeated]
    res.coverage = len(found) / len(interactive) if interactive else 0.0
    res.coverage_unconditional = len(found_uncond) / len(uncond) if uncond else 0.0
    res.stats = {
        "code_interactive": len(interactive),
        "code_unconditional": len(uncond),
        "matched": len(found),
        "matched_unconditional": len(found_uncond),
        "unmatched_code": len(res.unmatched_code),
        "observed_total": sum(len(p.elements) for p in pages),
        "observed_unmatched_visible": len(res.unmatched_observed),
    }
    return res
