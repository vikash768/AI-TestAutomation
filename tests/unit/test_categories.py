"""The checklist bucketing that drives the QA Categories dashboard."""
from qaplatform.execute.smoke import FindingRecord, TestRecord
from qaplatform.pipeline import CHECKLIST, _category_of_bucket, finalize_categories


def test_category_mapping():
    assert _category_of_bucket("security", "security") == "security"
    assert _category_of_bucket("ui-responsive", "responsive") == "ui"
    assert _category_of_bucket("accessibility", "dialog-a11y") == "ui"
    assert _category_of_bucket("performance", "load") == "load"
    assert _category_of_bucket("unit", "unit") == "unit"
    assert _category_of_bucket("functional", "smoke-click") == "functional"
    assert _category_of_bucket("validation", "validation") == "functional"
    assert _category_of_bucket("stability", "page-load") == "stability"
    assert set(CHECKLIST) == {"functional", "ui", "security", "load", "unit", "stability"}


def test_finalize_counts_per_bucket():
    tests = [
        TestRecord(test_key="a", name="", technique="security", category="security", route="/", status="fail"),
        TestRecord(test_key="b", name="", technique="responsive", category="ui-responsive", route="/", status="pass"),
        TestRecord(test_key="c", name="", technique="unit", category="unit", route="/", status="blocked"),
        TestRecord(test_key="d", name="", technique="load", category="performance", route="/", status="pass"),
    ]
    findings = [FindingRecord(fingerprint="f1", route="/", category="security", severity="medium", title="x")]
    stats = finalize_categories({}, tests, findings)
    c = stats["categories"]
    assert c["security"]["ran"] and c["security"]["fail"] == 1 and c["security"]["findings"] == 1
    assert c["ui"]["pass"] == 1 and c["ui"]["ran"]
    assert c["unit"]["blocked"] == 1
    assert c["load"]["pass"] == 1
    assert c["functional"]["ran"] is False  # nothing functional ran in this set
