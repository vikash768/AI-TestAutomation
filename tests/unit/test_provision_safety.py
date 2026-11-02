"""Provision must only ever act on its own marked record, whatever the delete UX looks like."""
import contextlib
from types import SimpleNamespace

import pytest

from qaplatform.execute.provision import Provisioner
from qaplatform.explore.browser import attach_monitor

ROWS = ["v0.2.14 real draft A", "v0.0.123451 qaprobet11", "v0.2.13 real draft B"]


def page_html(mode: str, rows=ROWS) -> str:
    # mode: direct (no confirm, like Life Sadx firmware), modal (custom dialog), native (window.confirm)
    return """<!doctype html><html><body><table><tbody id="tb"></tbody></table><script>
let rows = %r; const mode = %r;
function render(){ const tb=document.getElementById('tb'); tb.innerHTML='';
  rows.forEach((r,i)=>{ const tr=document.createElement('tr'); tr.innerHTML=`<td>${r}</td><td><button>Publish</button> <button class="del">Delete</button></td>`;
    tr.querySelector('.del').onclick=()=>ask(i); tb.appendChild(tr); }); }
function ask(i){
  if (mode==='direct') { rows.splice(i,1); render(); }
  else if (mode==='native') { if (confirm('Delete?')) { rows.splice(i,1); render(); } }
  else { const m=document.createElement('div'); m.setAttribute('role','dialog'); m.innerHTML='<p>Delete this?</p><button id="no">Cancel</button><button id="yes">Delete</button>';
    document.body.appendChild(m); m.querySelector('#yes').onclick=()=>{rows.splice(i,1); m.remove(); render();}; m.querySelector('#no').onclick=()=>m.remove(); } }
render();
</script></body></html>""" % (rows, mode)


@pytest.fixture()
def browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    yield b
    b.close()
    pw.stop()


def make(browser, mode, rows=ROWS):
    page = browser.new_page()
    page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=page_html(mode, rows)))
    page.goto("https://app.test/")
    runner = SimpleNamespace(page=page, mon=attach_monitor(page), guard=SimpleNamespace(suspended=contextlib.nullcontext),
                             settings=SimpleNamespace(action_timeout_ms=3000, settle_ms=150))
    return page, Provisioner(runner, lambda *a, **k: None, "t1")


def texts(page):
    return page.locator("tbody td:first-child").all_inner_texts()


@pytest.mark.parametrize("mode", ["direct", "modal", "native"])
def test_delete_removes_only_the_demo_record(browser, mode):
    page, pv = make(browser, mode)
    ok, why = pv._click_in_record("qaprobet11", "Delete", destroy=True)
    assert ok, why
    assert texts(page) == ["v0.2.14 real draft A", "v0.2.13 real draft B"]  # both real drafts untouched


def test_ambiguous_marker_is_refused(browser):
    page, pv = make(browser, "direct", ["qaprobet11 copy one", "qaprobet11 copy two", "real"])
    ok, why = pv._click_in_record("qaprobet11", "Delete", destroy=True)
    assert not ok and "2 marked records" in why
    assert len(texts(page)) == 3


def test_native_confirm_is_dismissed_outside_a_provision_delete(browser):
    page, pv = make(browser, "native")
    page.locator("tr").nth(0).locator(".del").click()  # some other click on a REAL row
    page.wait_for_timeout(150)
    assert len(texts(page)) == 3  # the confirm was dismissed: nothing deleted


def _stub(browser, html):
    page = browser.new_page()
    page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
    page.goto("https://app.test/")
    runner = SimpleNamespace(page=page, mon=attach_monitor(page), guard=SimpleNamespace(suspended=contextlib.nullcontext),
                             settings=SimpleNamespace(action_timeout_ms=3000, settle_ms=100), prepare=lambda url, rev=None: (True, ""))
    return page, Provisioner(runner, lambda *a, **k: None, "t1")


def test_missing_record_on_an_error_screen_is_not_proof_of_deletion(browser):
    _, pv = _stub(browser, "<html><body><p class='error-text'>Server error (429)</p></body></html>")
    gone, why = pv._confirm_absent("https://app.test/", [], "qaprobet11")
    assert not gone and why.startswith("deletion unverified")


def test_missing_record_in_a_rendered_list_is_deletion(browser):
    _, pv = _stub(browser, "<html><body><table><tbody><tr><td>v0.2.14 real</td><td><button>Delete</button></td></tr></tbody></table></body></html>")
    assert pv._confirm_absent("https://app.test/", [], "qaprobet11") == (True, "")


def test_open_create_form_is_never_the_marked_record(browser):
    from qaplatform.explore.scripts import MARKED_ROWS_JS
    page, _ = _stub(browser, """<html><body><form class="card composer-panel"><h2>Upload firmware</h2>
      <span>qaprobet11.bin</span><input type="text" value="qaprobet11"><button type="submit">Upload</button></form></body></html>""")
    assert page.evaluate(MARKED_ROWS_JS, "qaprobet11") == []


def test_success_toast_is_not_a_rejection():
    from qaplatform.execute.provision import SUCCESS_RE
    assert SUCCESS_RE.search("Firmware 0.0.232701 uploaded as a draft")
    assert not SUCCESS_RE.search("version must contain a semantic version such as 0.2.15")
