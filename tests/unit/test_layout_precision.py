"""Layout findings must match what the screenshot shows: geometry alone is not a bug."""
import pytest

from qaplatform.explore.scripts import LAYOUT_CHECK_JS

# Geometrically suspect but visually FINE — every one of these used to be flagged; none should be now.
FINE = """<!doctype html><html><body style="margin:0">
  <div style="overflow:hidden;width:200px;padding-right:60px;white-space:nowrap">Hi</div>            <!-- padding inflates scrollWidth, text fits -->
  <div style="position:relative;height:120px">
    <button style="position:absolute;left:10px;top:10px;width:80px;height:40px">A</button>
    <button style="position:absolute;left:30px;top:10px;width:80px;height:40px">B</button>
    <div style="position:absolute;left:0;top:0;width:300px;height:100px;background:#fff">overlay on top</div>  <!-- covered overlap -->
  </div>
  <a href="#" style="display:inline-block;padding:14px;font-size:8px">x</a>                          <!-- small text, padded hit area >=24 -->
  <span class="sr-only" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">Menu</span>
  <div style="overflow:hidden;width:100px"><div style="width:600px;height:10px">wide child, clipped by parent</div></div>
  <div style="position:relative;height:120px">
    <button style="position:absolute;left:10px;top:3000px;width:80px;height:40px">C</button>         <!-- overlapping pair far below the fold -->
    <button style="position:absolute;left:30px;top:3000px;width:80px;height:40px">D</button>
  </div>
</body></html>"""

# Genuinely broken — must still be caught.
BROKEN = """<!doctype html><html><body style="margin:0">
  <div style="overflow:hidden;width:40px;height:20px;white-space:nowrap">This text is far too long to fit the box</div>
  <div style="position:relative;height:80px">
    <button style="position:absolute;left:10px;top:20px;width:90px;height:40px">Submit</button>
    <button style="position:absolute;left:40px;top:20px;width:90px;height:40px">Cancel</button>
  </div>
  <button style="width:14px;height:14px;padding:0;margin-top:10px">x</button>
</body></html>"""

OVERFLOW = """<!doctype html><html><body style="margin:0"><div style="width:3000px;height:10px">very wide</div></body></html>"""


@pytest.fixture()
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    pg = b.new_context(viewport={"width": 900, "height": 700}).new_page()
    yield pg
    b.close()
    pw.stop()


def _check(page, html):
    page.set_content(html)
    page.wait_for_timeout(50)
    return page.evaluate(LAYOUT_CHECK_JS, {"checkTargets": True})


def test_visually_fine_layouts_produce_no_findings(page):
    r = _check(page, FINE)
    assert r["overflowing"] == [] and not r["horizontalOverflow"], r["overflowing"]
    assert r["overlaps"] == [], r["overlaps"]
    assert r["smallTargets"] == [], r["smallTargets"]
    assert r["clipped"] == [], r["clipped"]


def test_real_breakage_is_still_caught(page):
    r = _check(page, BROKEN)
    assert len(r["clipped"]) >= 1, "genuinely clipped text missed"
    assert len(r["overlaps"]) >= 1, "genuinely overlapping buttons missed"
    assert len(r["smallTargets"]) >= 1, "genuinely tiny button missed"


def test_real_horizontal_overflow_is_caught(page):
    r = _check(page, OVERFLOW)
    assert r["horizontalOverflow"] and len(r["overflowing"]) >= 1


# "Empty page" must mean visually empty, not just little text — an image-only or canvas page is full.
from qaplatform.explore.scripts import PAGE_STATE_JS


def _blank(state):
    # mirrors the predicate in execute/smoke.py
    return bool(state.get("rootEmpty")) or (int(state.get("bodyTextLength", 0)) < 30 and int(state.get("visibleContent", 0)) < 1)


def test_image_only_page_is_not_called_empty(page):
    page.set_content('<!doctype html><body style="margin:0"><img src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22200%22 height=%22120%22%3E%3Crect width=%22200%22 height=%22120%22 fill=%22%234af%22/%3E%3C/svg%3E"><button>Go</button></body>')
    page.wait_for_timeout(50)
    st = page.evaluate(PAGE_STATE_JS)
    assert st["bodyTextLength"] < 30 and st["visibleContent"] >= 1
    assert not _blank(st)


def test_truly_empty_page_is_still_flagged(page):
    page.set_content('<!doctype html><body style="margin:0"></body>')
    page.wait_for_timeout(50)
    assert _blank(page.evaluate(PAGE_STATE_JS))
