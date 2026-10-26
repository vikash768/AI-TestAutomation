"""Confidence scoring and the reporting gate.

A finding is reported only when its confidence clears the threshold;
everything else is kept as "needs_review" so nothing is silently dropped.
"""
from __future__ import annotations

REPORT_THRESHOLD = 0.7


def confidence(reproduced_n: int, reproduced_m: int, deterministic: bool = True, base: float = 0.9) -> float:
    if reproduced_m <= 0:
        return base if deterministic else 0.5
    ratio = reproduced_n / reproduced_m
    if ratio >= 1.0:
        return 0.95
    if ratio >= 0.66:
        return 0.8
    if ratio > 0:
        return 0.45
    return 0.1


def gate_status(conf: float, threshold: float = REPORT_THRESHOLD) -> str:
    return "reported" if conf >= threshold else "needs_review"
