"""Native runs: one or two codebases, report evidence links, backend discovery, load test,
QA test-data ownership, journey safety and a full journey on a simulated device."""
import http.server
import json
import os
import re
import threading
import time
from zipfile import ZipFile
from pathlib import Path

import pytest

from qaplatform.app.backend import backend_routes, base_urls, client_endpoints
from qaplatform.app.code import build_app_inventory
from qaplatform.app.device import Snapshot
from qaplatform.app.explore import AppExplorer, Screen
from qaplatform.app.journeys import Journey, JourneyRunner, Step, plan_from_code, to_tests, validate
from qaplatform.app.loadtest import LoadConfig, choose_targets, run_load_test
from qaplatform.app.testdata import QADataRegistry, owned_screen, row_owner
from qaplatform.execute.smoke import FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedElement, ObservedPage
from qaplatform.report.html import artifact_href, write_report

RUN = "abc123def456"


def _write(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
    return root


RN_APP = {
    "package.json": '{"name":"rn","dependencies":{"react-native":"0.80"}}',
    "src/Nav.tsx": """
import { SampleScreen } from './SampleScreen';
import { ConnectScreen } from './ConnectScreen';
import { ProgressScreen } from './ProgressScreen';
import { HistoryScreen } from './HistoryScreen';
import { LoginScreen } from './LoginScreen';
export function Nav() { return (<Stack.Navigator>
  <Stack.Screen name="Login" component={LoginScreen} />
  <Stack.Screen name="Sample" component={SampleScreen} />
  <Stack.Screen name="Connect" component={ConnectScreen} />
  <Stack.Screen name="Progress" component={ProgressScreen} />
  <Stack.Screen name="History" component={HistoryScreen} />
</Stack.Navigator>); }
""",
    "src/LoginScreen.tsx": """
export function LoginScreen({ navigation }) { return (<View>
  <TextField label="Username" value={u} onChangeText={setU} />
  <TextField label="Password" value={p} onChangeText={setP} secureTextEntry />
  <Button title="Sign In" onPress={() => navigation.navigate('Sample')} />
</View>); }
""",
    "src/SampleScreen.tsx": """
export function SampleScreen({ navigation }) {
  const submit = () => { actions.saveDraft({ id }); navigation.navigate('Connect'); };
  return (<View>
    <TextField label="Test ID" required maxLength={32} value={id} onChangeText={setId} />
    <TextField label="Notes" maxLength={500} value={n} onChangeText={setN} />
    <TextField label="Operator" value={op} readOnly />
    <Button title="Continue" onPress={submit} />
  </View>); }
""",
    "src/ConnectScreen.tsx": """
export function ConnectScreen({ navigation }) {
  return (<View><Button title={checking ? 'Checking' : 'Continue'} onPress={() => navigation.navigate('Progress')} /></View>); }
""",
    "src/ProgressScreen.tsx": """
export function ProgressScreen() { useEffect(() => { const r = actions.addResult(run); }, []); return <View />; }
""",
    "src/HistoryScreen.tsx": """
export function HistoryScreen() { return (<View>
  <Button title="Sync All" onPress={() => syncTests()} />
  <Button title="Delete result" onPress={() => remove(id)} />
</View>); }
""",
    "src/autoSync.ts": """
export function startAutoSync() { const t = setInterval(attempt, RETRY_MS); AppState.addEventListener('change', () => syncTests()); }
""",
}


# -- one or two codebases -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from qaplatform.api import app as app_module
    return TestClient(app_module.app)


def test_parse_sources():
    from qaplatform.api.app import parse_sources
    assert parse_sources("https://github.com/a/app.git") == ["https://github.com/a/app.git"]
    assert parse_sources("https://github.com/a/app.git\r\n\n /srv/backend \n") == ["https://github.com/a/app.git", "/srv/backend"]
    assert parse_sources("https://github.com/a/app.git, https://github.com/a/be.git") == ["https://github.com/a/app.git", "https://github.com/a/be.git"]
    assert parse_sources("x\nx") == ["x"]


def test_native_run_with_two_sources_and_web_with_one(client):
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    r = client.post("/runs", data={"platform": "macos", "repo_url": "https://github.com/o/mobile.git\n/home/me/backend", "app_path": "/tmp/Fixture.app",
                                   "journeys": "true", "load_test": "true", "load_test_base_url": "https://api.example.net"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.get(Run, run_id)
        assert run.repo_url == "https://github.com/o/mobile.git"  # primary stays in repo_url
        assert run.options["codebase_sources"] == [{"role": "frontend", "source": "https://github.com/o/mobile.git"}, {"role": "backend", "source": "/home/me/backend"}]
        assert run.options["load_test"] is True and run.options["journeys"] is True and run.options["remote_qa_data"] is False
    # single source native: unchanged shape
    r = client.post("/runs", data={"platform": "macos", "repo_url": "/home/me/mobile", "app_path": "com.x"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        assert db.get(Run, run_id).options["codebase_sources"] == [{"role": "frontend", "source": "/home/me/mobile"}]
    # web: one codebase, options exactly as before
    r = client.post("/runs", data={"repo_url": "https://github.com/o/web.git", "live_url": "https://example.com"}, follow_redirects=False)
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        assert set(db.get(Run, run_id).options) == {"max_pages", "llm_intent", "phase2", "branch", "clean_start"}
    assert client.post("/runs", data={"repo_url": "a\nb", "live_url": "https://example.com"}).status_code == 400
    assert client.post("/runs", data={"platform": "macos", "repo_url": "a\nb\nc", "app_path": "x"}).status_code == 400
    page = client.get("/").text
    assert "Repository URL or local path" in page and "<textarea" in page


# -- report evidence links ------------------------------------------------------------------------------
def _native_stats():
    return {"platform": "android", "code_interactive": 10, "matched_interaction": 5, "coverage_interaction": 0.5, "tested_components": 4, "tested_ratio": 0.8, "screens": 3,
            "tests_by_status": {"pass": 3, "fail": 1, "blocked": 1}, "taps": 7, "safe_actions": 9, "held_back": 2,
            "held_actions": [{"screen": "History", "label": "Sync All", "class": "mutating", "reason": "changes or sends data"}],
            "journeys": {"planner": "code", "total": 1, "completed": 1, "items": [{"name": "Fill Sample", "source": "code", "kind": "create", "status": "pass", "done": 4, "steps": 4, "reason": "ok"}]},
            "qa_data": {"created": 1, "cleaned": 1, "leftover": 0, "records": [{"id": "qa_x", "entity": "field 'Test ID'", "where": "Sample", "journey": "Fill Sample", "status": "cleaned", "note": ""}]},
            "load_test": {"status": "pass", "base_url": "https://api.example.net", "users": 5, "max_rps": 10, "duration_s": 10, "p95_limit_ms": 1000, "max_error_rate": 0.01,
                          "endpoints": [{"method": "GET", "path": "/api/health", "url": "https://api.example.net/api/health", "status": "pass", "total": 90, "ok": 90, "failed": 0,
                                         "error_rate": 0.0, "rps": 9.0, "p50_ms": 20, "p95_ms": 40, "p99_ms": 60, "max_ms": 80, "duration_s": 10}]}}


def test_report_links_resolve_and_cannot_escape(tmp_path):
    from qaplatform.config import Settings
    artifacts = Settings.from_env().artifacts_dir
    run_id = "linktest0001"
    run_dir = artifacts / "runs" / run_id
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    shot = run_dir / "screenshots" / "001 home screen.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    assert artifact_href(str(shot), run_dir, run_id) == f"/artifacts/runs/{run_id}/screenshots/001%20home%20screen.png"
    assert artifact_href("screenshots/001 home screen.png", run_dir, run_id).endswith("/screenshots/001%20home%20screen.png")  # relative to the run folder
    cwd_rel = os.path.relpath(shot, Path.cwd())  # how the device stores screenshots: artifacts/runs/<id>/screenshots/...
    assert artifact_href(cwd_rel, run_dir, run_id).endswith("/screenshots/001%20home%20screen.png"), cwd_rel
    assert artifact_href(str(outside), run_dir, run_id) == ""  # outside the run folder
    assert artifact_href("../../../etc/passwd", run_dir, run_id) == ""  # traversal
    assert artifact_href(str(run_dir / "screenshots" / "missing.png"), run_dir, run_id) == ""  # no misleading link
    finding = FindingRecord(fingerprint="f1", route="Home", category="functional", severity="high", title="Broken <control>", evidence=[{"type": "screenshot", "path": str(shot), "viewport": "device"},
                                                                                                                              {"type": "screenshot", "path": str(outside)}])
    page = ObservedPage(route="Home", url="app://x/Home", load_ms=20, viewports={"device": {"screenshot": str(shot)}}, screenshot=str(shot))
    test = TestRecord(test_key="t", name="Journey: x", technique="journey", category="functional", route="Home", status="fail", evidence=[{"type": "screenshot", "path": str(shot)}],
                      expected="Shows <sample details>", observed="Loading stopped", duration_ms=42, code_ref="Sample.tsx:12", qa_data="qa_x", cleanup="qa_x: cleaned",
                      steps=[{"action": "type", "target": "Test ID", "value": "qa_x", "result": "typed"}])
    passing_test = TestRecord(test_key="t2", name="Passing sample check", technique="journey", category="functional", route="Home")
    write_report(run_dir, run_id, {"live_url": "android: com.x"}, _native_stats(), [page], [], [passing_test, test], [finding])
    html = (run_dir / "report.html").read_text()
    hrefs = set(re.findall(r'href="(/artifacts/[^"]+)"', html))
    assert hrefs == {f"/artifacts/runs/{run_id}/screenshots/001%20home%20screen.png"}
    for section_id in ("findings", "tests", "screens"):
        assert f'href="#{section_id}"' in html
        assert f'id="{section_id}"' in html
    assert html.index('id="screens"') < html.index('id="findings"')
    assert f'<img src="/artifacts/runs/{run_id}/screenshots/001%20home%20screen.png"' in html
    assert 'name="viewport"' in html and 'class="finding-card' in html
    assert 'id="finding-search"' in html and 'id="finding-severity"' in html
    assert "Broken &lt;control&gt;" in html and "Broken <control>" not in html
    assert str(outside) not in html and "Screenshot missing" in html
    failed_test = html.split('<details class="test-result">', 2)[1]
    assert "Journey: x" in failed_test and "functional · journey · Home" in failed_test
    for detail in ("Shows &lt;sample details&gt;", "Loading stopped", "42 ms", "Sample.tsx:12", "qa_x: cleaned", "type Test ID qa_x → typed", "/screenshots/001%20home%20screen.png"):
        assert detail in failed_test
    from fastapi.testclient import TestClient
    from qaplatform.api import app as app_module
    c = TestClient(app_module.app)
    assert c.get(f"/runs/{run_id}/report").status_code == 200
    for h in hrefs:  # every link in the report opens
        assert c.get(h.replace("%20", " ")).status_code == 200, h
    for section in ("Performance &amp; Load Testing", "Mobile device performance", "Backend API load test", "Journeys and QA test data", "Tested vs held back",
                    "full journeys completed", "QA records cleaned", "Screen load time"):
        assert section in html, section


@pytest.mark.parametrize("platform", ["android", "ios", "macos"])
def test_device_report_groups_component_results_with_screens(tmp_path, platform):
    from qaplatform.execute.smoke import ComponentRecord

    pages = [ObservedPage(route=route, url=f"app://qa/{route}", title="Example app") for route in ("Library", "Settings")]
    components = [
        ComponentRecord(key="open", route="Library", kind="button", label="Open item", source="observed"),
        ComponentRecord(key="toggle", route="(layout)", kind="switch", label="Show previews", source="both", page_url=pages[1].url),
        ComponentRecord(key="remove", route="Settings", kind="button", label="Remove item", source="observed", status="skipped"),
    ]
    tests = [
        TestRecord("open|ok", "Open selected item", "smoke", "functional", "Library", component_key="open"),
        TestRecord("toggle|on", "Enable previews", "state-change", "functional", "Settings", status="fail", component_key="toggle"),
        TestRecord("toggle|off", "Disable previews", "state-change", "functional", "Settings", component_key="toggle"),
        TestRecord("launch", "Start app", "launch", "platform", "Library", status="blocked"),
    ]
    out = write_report(tmp_path, "screenreport", {}, {"platform": platform}, pages, components, tests, [])
    html = out.read_text()
    cards = re.findall(r'<article class="screen-card".*?</article>', html, re.DOTALL)
    assert len(cards) == 2
    library, settings = cards
    assert all(text in library for text in ("Library</h3>", "Open item", "1 passed", "Open selected item"))
    assert all(text in settings for text in ("Settings</h3>", "Show previews", "1 failed", "1 passed", "Enable previews", "Skipped"))
    assert all(text not in library for text in ("Show previews", "Enable previews", "Start app"))
    assert "Open selected item" not in settings and all("Screenshot unavailable" in card for card in cards)
    assert '<h2 id="tests">Other checks</h2>' in html and html.count("Start app") == 2  # Other checks and performance


def test_web_report_keeps_its_layout(tmp_path):
    out = write_report(tmp_path, "web1", {"live_url": "https://x"}, {"coverage": 0.5, "code_interactive": 2, "matched": 1}, [], [], [], [])
    html = out.read_text()
    assert "components in code" in html and "Page load time" in html
    assert "Load Testing" not in html and "Journeys" not in html and "<h2>Tests</h2>" not in html


# -- backend discovery ----------------------------------------------------------------------------------
BACKEND = {
    "package.json": '{"name":"be","dependencies":{"express":"5"}}',
    "src/app.ts": """
import express from 'express';
import resultsRouter from './routes/results';
import authRouter from './routes/auth';
const app = express();
app.set('trust proxy', 1);
app.get('/api/health', (req, res) => res.json({ status: 'ok' }));
app.use('/api/results', requireAuth, resultsRouter);
app.use('/api/auth', authRouter);
""",
    "src/routes/results.ts": """
const router = Router();
router.use(requireAuth);
router.get('/', listResults);
router.get('/:id', getResult);
router.post('/', createResults);
router.delete('/:id', removeResult);
export default router;
""",
    "src/routes/auth.ts": """
const router = Router();
router.post('/login', login);
router.get('/me', authenticate, me);
export default router;
""",
    "py/main.py": """
router = APIRouter(prefix="/v2")
@router.get("/items")
def items(user=Depends(get_current_user)): ...
@router.get("/status")
def status(): ...
""",
}


def test_backend_routes_with_mounts_and_auth(tmp_path):
    routes = {(r.method, r.path): r for r in backend_routes(_write(tmp_path, BACKEND))}
    assert ("GET", "/api/health") in routes and not routes[("GET", "/api/health")].auth
    assert ("GET", "/api/results") in routes and routes[("GET", "/api/results")].auth
    assert ("GET", "/api/results/:id") in routes and ("DELETE", "/api/results/:id") in routes
    assert ("POST", "/api/auth/login") in routes and routes[("GET", "/api/auth/me")].auth
    assert routes[("GET", "/v2/items")].auth and ("GET", "/v2/status") in routes
    assert not any(p == "trust proxy" for _, p in routes)


def test_client_endpoints_and_base_url(tmp_path):
    root = _write(tmp_path, {"src/api/client.ts": """
export const SERVER_URL = 'https://api.example.net';
const DEV_URL = 'http://localhost:3000';
async function request<T>(baseUrl: string, path: string, options = {}) { return fetch(`${base}/api${path}`, { method: options.method ?? 'GET' }); }
export const api = {
  health: (b: string) => request<{ status: string }>(b, '/health'),
  list: (b: string, p) => request<ResultPage>(b, `/results${query(p)}`, { token }),
  one: (b: string, id: string) => request<{ r: R }>(b, `/results/${encodeURIComponent(id)}`, { token }),
  upload: (b: string, body) => request<U>(b, '/results', { method: 'POST', body }),
};
"""})
    eps = {(e.method, e.path): e for e in client_endpoints(root)}
    assert ("GET", "/api/health") in eps and ("GET", "/api/results") in eps and ("POST", "/api/results") in eps
    assert any(e.dynamic for e in eps.values())
    assert base_urls(root) == ["https://api.example.net"]


# -- load test ------------------------------------------------------------------------------------------
class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"{}"
        if self.path == "/fixture-record":
            code, delay = (200 if self.headers.get("Authorization") == "Bearer fixture-api-token" else 401), 0
            body = b'{"record": {"available": true}, "other": "fixture-only-body"}'
        else:
            code, delay = {"/api/health": (200, 0), "/api/slow": (200, 0.25), "/api/err": (500, 0), "/api/private": (401, 0)}.get(self.path, (404, 0))
        time.sleep(delay)
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def api_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_load_targets_are_get_only_and_without_ids():
    from qaplatform.app.backend import BackendRoute, ClientEndpoint
    cfg = LoadConfig(enabled=True, base_url="http://x", max_endpoints=5)
    targets, skipped = choose_targets(cfg, [BackendRoute("GET", "/api/private", "a", 1, auth=True), BackendRoute("DELETE", "/api/x/:id", "a", 2)],
                                      [ClientEndpoint("GET", "/api/health", "c", 1), ClientEndpoint("POST", "/api/results", "c", 2), ClientEndpoint("GET", "/api/results/${id}", "c", 3, dynamic=True)])
    assert [t["path"] for t in targets] == ["/api/health"]
    text = " ".join(skipped)
    assert "POST /api/results: not idempotent" in text and "needs an id" in text and "behind sign-in" in text and "DELETE" in text


def test_load_test_metrics_thresholds_and_caps(api_server):
    events = []
    cfg = LoadConfig(enabled=True, base_url=api_server, users=4, duration_s=6, max_rps=25, p95_limit_ms=150, endpoints=["/api/health", "/api/slow", "/api/err", "/api/private", "POST /api/x"], max_endpoints=5).capped()
    summary, tests, findings = run_load_test(cfg, [], [], lambda *a, **k: events.append(a), run_id=RUN)
    eps = {e["path"]: e for e in summary["endpoints"]}
    assert set(eps) == {"/api/health", "/api/slow", "/api/err"}
    h = eps["/api/health"]
    assert h["status"] == "pass" and h["total"] > 10 and h["failed"] == 0 and h["p50_ms"] <= h["p95_ms"] <= h["p99_ms"] <= h["max_ms"]
    assert h["rps"] <= 25 * 1.2  # the shared limiter holds the rate
    assert eps["/api/slow"]["status"] == "fail" and eps["/api/err"]["error_rate"] == 1.0
    titles = " ".join(f.title for f in findings)
    assert "Backend errors under load: GET /api/err" in titles and "Slow backend under load: GET /api/slow" in titles
    assert any("/api/private: answers 401" in s for s in summary["skipped"]) and any("POST /api/x" in s for s in summary["skipped"])
    assert summary["status"] == "fail" and {t.technique for t in tests} == {"load"}
    assert LoadConfig(users=999, max_rps=999, duration_s=99999).capped().users == 50


def test_load_test_disabled_or_without_target():
    s, t, f = run_load_test(LoadConfig(enabled=False), [], [], lambda *a, **k: None)
    assert s["status"] == "disabled" and not t
    s, t, f = run_load_test(LoadConfig(enabled=True), [], [], lambda *a, **k: None)
    assert s["status"] == "blocked" and t[0].status == "blocked" and "LOAD_TEST_BASE_URL" in s["reason"]


# -- QA data ownership ----------------------------------------------------------------------------------
LIST_XML = """<hierarchy width="1080" height="2340">
 <node class="android.view.ViewGroup" package="p" clickable="true" content-desc="Test ID QA-ABC123-101010, Valid" bounds="[0,300][1080,500]">
  <node class="android.widget.TextView" package="p" text="QA-ABC123-101010" bounds="[40,320][600,380]"/>
  <node class="android.widget.Button" package="p" content-desc="Delete" clickable="true" bounds="[900,320][1040,380]"/>
 </node>
 <node class="android.view.ViewGroup" package="p" clickable="true" content-desc="Test ID LS-0042, Valid" bounds="[0,520][1080,720]">
  <node class="android.widget.TextView" package="p" text="LS-0042" bounds="[40,540][600,600]"/>
  <node class="android.widget.Button" package="p" content-desc="Delete" clickable="true" bounds="[900,540][1040,600]"/>
 </node>
</hierarchy>"""


def test_registry_values_and_row_ownership(tmp_path):
    reg = QADataRegistry(RUN, tmp_path / "qa_data.json")
    v = reg.value()
    assert v.startswith(f"qa_{RUN}_") and reg.is_ours(v) and reg.is_ours(v.upper()) and reg.is_ours(reg.value(style=1))
    assert not reg.is_ours("LS-0042") and len(reg.value(max_len=12)) <= 12 and reg.is_ours(reg.value(max_len=12))
    assert reg.resolve("{empty}") == "" and len(reg.resolve("{long:42}")) == 42 and reg.resolve("{number}") == "1" and reg.resolve("ab") == "ab"
    from qaplatform.app.device import parse_android
    snap = parse_android(LIST_XML, "p")
    ours, theirs = [e for e in snap.elements if e.name == "Delete"]
    assert row_owner(snap, ours, reg) and row_owner(snap, theirs, reg) is None
    assert not owned_screen(snap, reg)  # a list holding someone else's record is not ours
    rec = reg.add(v, "field 'Test ID'", "Sample", "j", kind="field", original="LS-0042 private")
    ledger = json.loads((tmp_path / "qa_data.json").read_text())
    assert ledger["records"][0]["original"] is True and "LS-0042 private" not in json.dumps(ledger)  # the user's value never lands in the report
    reg.set_status(rec, "cleaned")
    assert reg.summary()["cleaned"] == 1 and not reg.pending()


# -- journey safety -------------------------------------------------------------------------------------
def test_plan_and_safety_validation(tmp_path):
    root = _write(tmp_path, RN_APP)
    inv, _ = build_app_inventory(root, "android")
    fields = {e.label: e for e in inv.elements if e.kind == "input"}
    assert {"Test ID", "Notes"} <= set(fields) and "Operator" not in fields  # read-only fields are not inputs
    plan = plan_from_code(inv, set())
    names = [j.name for j in plan]
    assert "Fill and submit Sample with QA data" in names and not any("Login" in n for n in names)  # sign-in screens are left to the sign-in tests
    assert any("keeps at most 32" in n for n in names) and any("empty 'Test ID' is refused" in n for n in names)
    labels = {"continue", "sync all", "delete result", "test id", "notes"}
    validate(plan, inv, {}, root, labels)
    create = next(j for j in plan if j.kind == "create")
    assert not create.blocked and create.steps[-1].effect == "local_draft"
    # the LLM claims 'Continue' on Connect changes nothing; the code says Progress stores a record and the app uploads in the background
    j = Journey("ai", "create", "ai", [Step("open", "Sample"), Step("type", "Sample", "Test ID", "{qa}"), Step("submit", "Sample", "Continue"),
                                       Step("submit", "Connect", "Continue", effect="none", oracle="ai"), Step("verify_text", "History", value="{qa}", oracle="ai")])
    validate([j], inv, {}, root, labels)
    assert "send QA data to the backend" in j.blocked and [s.action for s in j.steps] == ["open", "type", "submit"]
    j2 = Journey("x", "create", "ai", [Step("open", "Sample"), Step("type", "Sample", "Test ID", "{qa}"), Step("submit", "Sample", "Continue"), Step("submit", "Connect", "Continue")])
    validate([j2], inv, {}, root, labels, allow_remote=True)
    assert not j2.blocked  # with the explicit opt-in the journey may go on
    bulk = Journey("b", "flow", "ai", [Step("open", "History"), Step("tap", "History", "Sync All")])
    delete = Journey("d", "delete", "ai", [Step("open", "History"), Step("tap", "History", "Delete result")])
    ghost = Journey("g", "flow", "ai", [Step("open", "History"), Step("tap", "History", "Buy premium")])
    leak = Journey("l", "create", "ai", [Step("open", "Sample"), Step("type", "Sample", "Test ID", "hunter2-secret")])
    validate([bulk, delete, ghost, leak], inv, {}, root, labels, secrets=("hunter2-secret",))
    assert "bulk" in bulk.blocked and "nothing before it creates" in delete.blocked and "not a control" in ghost.blocked
    assert leak.steps[1].value == "{qa}"  # a credential is never typed outside the sign-in form


# -- a full journey on a simulated device ---------------------------------------------------------------
class FakeDevice:
    """Home -> (New Test) -> Sample form -> (Continue) -> Connect. Test ID: upper-cased, maxLength 32, 4-32 letters/digits/dashes, kept as a draft."""
    platform = "android"

    def __init__(self, run_dir: Path):
        self.screen, self.value, self.draft, self.error, self.run_dir, self.n = "Home", "", "LS-0007", "", run_dir, 0
        self.box = ObservedElement(selector="id:tid", role="textbox", name="Test ID", tag="EditText", bbox=[40, 400, 1000, 100])

    def snapshot(self, settle=0.0) -> Snapshot:
        if self.screen == "Home":
            return Snapshot(package="p", elements=[ObservedElement(selector="~New Test", role="button", name="New Test", tag="Button", bbox=[40, 300, 1000, 100])], texts=["Home"], width=1080, height=2340)
        if self.screen == "Sample":
            return Snapshot(package="p", elements=[self.box, ObservedElement(selector="~Continue", role="button", name="Continue", tag="Button", bbox=[40, 2000, 1000, 120])],
                            texts=["Sample Details"] + ([self.error] if self.error else []), width=1080, height=2340)
        return Snapshot(package="p", elements=[ObservedElement(selector="~Refresh devices", role="button", name="Refresh devices", tag="Button", bbox=[40, 300, 200, 100])],
                        texts=["Connect Device", "Power on your LifeSADX device and connect it to the phone"], width=1080, height=2340)

    def tap(self, el) -> bool:
        if el.name == "New Test" and self.screen == "Home":
            self.screen, self.value, self.error = "Sample", self.draft, ""
        elif el.name == "Continue" and self.screen == "Sample":
            if not self.value:
                self.error = "Test ID is required"
            elif not re.fullmatch(r"[A-Z0-9-]{4,32}", self.value):
                self.error = "Use 4–32 letters, numbers or dashes"
            else:
                self.draft, self.screen, self.error = self.value, "Connect", ""
        return True

    def type_into(self, el, value) -> bool:
        self.value, self.error = value.upper()[:32], ""
        return True

    def read_value(self, el) -> str:
        return self.value if self.screen == "Sample" else ""

    def back(self):
        self.screen = {"Connect": "Sample", "Sample": "Home"}.get(self.screen, "Home")
        if self.screen == "Sample":
            self.value = self.draft

    def find_text(self, text, scroll=True):
        return text in self.value

    def screenshot(self, tag):
        self.n += 1
        p = self.run_dir / f"{self.n:03d}.png"
        p.write_bytes(b"png")
        return str(p)

    def hide_keyboard(self): pass
    def scroll_into_view(self, el): pass
    def app_state(self): return 4
    def errors_since_last(self): return []
    def activate(self): pass
    def relaunch(self): self.screen = "Home"


def test_full_journey_with_format_retry_and_cleanup(tmp_path):
    dev = FakeDevice(tmp_path)
    ex = AppExplorer(dev, lambda *a, **k: None)
    home = Screen(sig="h", snap=dev.snapshot(), route="Home")
    dev.screen = "Sample"
    sample = Screen(sig="s", snap=dev.snapshot(), path=[home.snap.elements[0]], depth=1, route="Sample")
    dev.screen = "Home"
    ex.screens, ex.home = [home, sample], home
    root = _write(tmp_path / "app", RN_APP)
    inv, _ = build_app_inventory(root, "android")
    reg = QADataRegistry(RUN, tmp_path / "qa_data.json")
    runner = JourneyRunner(dev, ex, reg, inv, lambda *a, **k: None, {})
    plan = validate(plan_from_code(inv, {"Sample"}), inv, {}, root, {"continue", "test id", "notes", "new test"})
    results = {r.journey.name: r for r in runner.run_all(plan)}
    create = results["Fill and submit Sample with QA data"]
    assert create.status == "pass", create.reason
    assert re.fullmatch(r"QA-ABC123DEF456-\d{6}", dev.draft)  # qa_..._ was refused by the format rule, the dash style went through
    assert results["Sample: 'Test ID' keeps at most 32 characters"].status == "pass"
    assert results["Sample: empty 'Test ID' is refused"].status == "pass"
    # cleanup puts the draft back to what it was before the run
    runner.cleanup()
    assert dev.draft == "LS-0007"
    summary = reg.summary()
    assert summary["created"] >= 1 and summary["leftover"] == 0, summary
    tests = to_tests(list(results.values()), reg)
    t = next(t for t in tests if t.name.startswith("Journey: Fill and submit"))
    assert t.status == "pass" and "QA-" in t.qa_data.upper() and "cleaned" in t.cleanup and t.evidence and t.steps[0]["action"] == "open"


def test_hardware_screen_blocks_instead_of_failing(tmp_path):
    dev = FakeDevice(tmp_path)
    dev.screen = "Connect"
    ex = AppExplorer(dev, lambda *a, **k: None)
    connect = Screen(sig="c", snap=dev.snapshot(), route="Connect")
    ex.screens, ex.home = [connect], connect
    inv, _ = build_app_inventory(_write(tmp_path / "app", RN_APP), "android")
    runner = JourneyRunner(dev, ex, QADataRegistry(RUN), inv, lambda *a, **k: None, {})
    r = runner.run(Journey("Start a test", "flow", "ai", [Step("open", "Connect"), Step("submit", "Connect", "Refresh devices", effect="none")]))
    assert r.status == "blocked" and "hardware" in r.reason


# -- the whole native pipeline, Appium replaced by the simulated device ----------------------------------
class PipelineDevice(FakeDevice):
    def __init__(self, settings, build, run_dir, events, clean_start=False):
        super().__init__(run_dir)
        self.platform = build.platform
        self.build, self.launch_seconds = build, 1.0
        self.driver = type("D", (), {"capabilities": {"deviceScreenDensity": 420}})()

    def start(self): pass
    def stop(self): pass
    def toast(self): return ""
    def rotate(self, landscape): return False
    def set_network(self, on): return False
    def background(self, seconds=3): pass
    def keys(self, keys): pass
    def undo(self): pass


@pytest.mark.parametrize(("mode", "unavailable", "expected_value"), [
    ("backend", "", "true"), ("backend", "", "false"), ("full", "", "true"),
    ("full", "checkout", "true"), ("full", "device", "true"),
])
@pytest.mark.parametrize("platform", ["android", "macos"])
def test_app_modes_report_requested_scope(client, tmp_path, monkeypatch, api_server, mode, unavailable, expected_value, platform):
    from qaplatform.api import app as app_module
    from qaplatform.app import pipeline as app_pipeline
    from qaplatform.config import Settings
    from qaplatform.db.models import Component, Page, Run, Test
    from qaplatform.db.session import init_db, session
    from qaplatform.ingest.repo import RepoInfo
    from qaplatform.worker import claim_next, execute

    repo_url = "https://github.com/example/backend.git"
    git_token = "fixture-per-run-token"
    backend = _write(tmp_path / "backend", BACKEND)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'run.db'}",
        artifacts_dir=tmp_path / "artifacts",
        secret_key="fixture-mode-key",
    )
    init_db(settings.database_url)
    for key in ("database_url", "artifacts_dir", "secret_key"):
        monkeypatch.setattr(app_module.settings, key, getattr(settings, key))
    monkeypatch.setenv("GITHUB_TOKEN", "ignored-global-token")
    monkeypatch.setenv("LOAD_TEST_ENABLED", "true")
    monkeypatch.setenv("LOAD_TEST_BASE_URL", "https://fixture-never-request.invalid")

    fetches = []

    def fetch_fixture(source, dest, *, token, branch):
        assert source == repo_url and token == git_token and branch == "review"
        fetches.append(dest)
        if unavailable == "checkout":
            raise RuntimeError(f"Git rejected {token}")
        return RepoInfo(path=backend, commit_sha="a" * 40)

    class UnavailableDevice(PipelineDevice):
        def start(self):
            raise RuntimeError("Fixture Appium is unavailable")

    def unexpected_device(*args, **kwargs):
        pytest.fail("Backend testing must complete without preparing or starting an app")

    def disabled_load_only(config, *args):
        assert config.enabled is False
        assert not config.bearer_token
        return run_load_test(config, *args)

    monkeypatch.setattr(app_pipeline, "fetch_repo", fetch_fixture)
    monkeypatch.setattr(app_pipeline, "run_load_test", disabled_load_only)
    device = UnavailableDevice if unavailable == "device" else PipelineDevice
    monkeypatch.setattr(app_pipeline, "AppDevice", device if mode == "full" else unexpected_device)
    if mode == "backend":
        monkeypatch.setattr(app_pipeline, "resolve_build", unexpected_device)

    target = {"app_path": "com.example.rn", "ui_expected_text": "Home"} if mode == "full" else {}
    api_token = "fixture-api-token"
    response = client.post("/runs", data={
        "platform": platform, "test_mode": mode, "project": "App QA", "repo_url": repo_url, "github_token": git_token,
        "branch": "review", "api_base_url": api_server, "api_path": "/fixture-record", "api_json_field": "record.available",
        "api_expected_value": expected_value, "api_token": api_token, **target,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    assert claim_next(settings.database_url, (platform,)) == (run_id, platform)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        assert git_token not in run.secrets and api_token not in run.secrets and api_token not in str(run.options)

    execute(run_id, platform, settings)

    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        assert run.status == "completed", (run.error, run.message)
        assert run.secrets is None
        assert run.commit_sha == ("" if unavailable == "checkout" else "a" * 40)
        assert run.stats["platform"] == platform
        assert run.stats["test_mode"] == mode and run.stats["code_available"] is False
        assert run.stats["load_test"]["status"] == "disabled"
        if platform == "macos" and mode == "full" and unavailable != "device":
            assert run.stats["desktop_coverage"]["states_walked"] > 0
        assert (run.stats["backend"]["routes"] >= 6) == (unavailable != "checkout")
        api_status = "pass" if expected_value == "true" else "fail"
        assert run.stats["legs"]["backend"]["status"] == ("blocked" if unavailable == "checkout" else api_status)
        assert [src["role"] for src in run.stats["sources"]] == ["backend"]
        tests = db.query(Test).filter_by(run_id=run_id).all()
        by_key = {t.test_key: t for t in tests}
        assert by_key["backend|route-inventory"].status == ("blocked" if unavailable == "checkout" else "pass")
        assert by_key["assertion|api"].status == api_status
        assert all(t.technique != "load" for t in tests)
        pages = db.query(Page).filter_by(run_id=run_id).all()
        components = db.query(Component).filter_by(run_id=run_id).all()
        if mode == "full" and unavailable != "device":
            assert pages and components
            assert all(c.source == "observed" and not c.code_file for c in components)
            assert any(t.technique == "smoke" for t in tests)
            assert by_key["assertion|ui"].status == "pass"
            assert by_key["full|assertions"].status == "pass"
            assert run.stats["legs"]["frontend"]["status"] != "blocked"
        elif mode == "full":
            assert not pages and not components
            assert run.stats["legs"]["frontend"]["status"] == "blocked"
            assert by_key["assertion|ui"].status == by_key["full|assertions"].status == "blocked"
        else:
            assert not pages and not components
            assert "frontend" not in run.stats["legs"]
    assert len(fetches) == 1
    checkout = fetches[0]
    assert checkout.name == "backend" and checkout.parent.parent == tmp_path / ".artifacts-repos"
    assert not checkout.parent.exists()

    run_dir = settings.artifacts_dir / "runs" / run_id
    backend_inventory = (run_dir / "backend.json").read_text()
    html = (run_dir / "report.html").read_text()
    assert ('"/api/health"' in backend_inventory) == (unavailable != "checkout")
    run_view = client.get(f"/runs/{run_id}").text
    for private_value in (git_token, api_token, "fixture-only-body"):
        assert all(private_value not in artifact for artifact in (backend_inventory, html, run_view))
    assert f"{mode.capitalize()} testing" in html
    assert "Backend source review" in html and "Open backend route inventory" in html
    assert ("<h2>Screens</h2>" in html) == (mode == "full" and unavailable != "device")
    assert ('id="desktop-coverage"' in html) == (platform == "macos" and mode == "full" and unavailable != "device")
    if platform == "macos" and mode == "full" and unavailable != "device":
        assert "Desktop coverage by control type" in html


@pytest.mark.parametrize("kind", ["apk", "installed", "upload", "mac-app"])
def test_native_pipeline_without_source_reports_device_findings(tmp_path, monkeypatch, kind):
    from prometheus_client import generate_latest

    from qaplatform.app import build, buildcheck, pipeline as app_pipeline
    from qaplatform.app.uploads import apk_upload_path
    from qaplatform.config import Settings
    from qaplatform.db.models import Component, Finding, Run, Test
    from qaplatform.db.session import init_db, session
    from qaplatform.report.metrics import build_registry

    class DefectiveDevice(PipelineDevice):
        def snapshot(self, settle=0.0):
            observed = super().snapshot(settle)
            if self.screen == "Home":
                observed.texts.append("Last test: Invalid Date")
            return observed

    def unexpected(*args, **kwargs):
        pytest.fail("Source-free app runs should skip Git and source-based planning")

    monkeypatch.setattr(app_pipeline, "AppDevice", DefectiveDevice)
    monkeypatch.setattr(app_pipeline, "fetch_repo", unexpected)
    monkeypatch.setattr(app_pipeline, "plan_from_code", unexpected)
    monkeypatch.setattr(app_pipeline.native_ai, "plan", unexpected)
    monkeypatch.setattr(build, "_aapt2", lambda: None)
    monkeypatch.setattr(buildcheck, "_aapt2", lambda: None)
    platform = "macos" if kind == "mac-app" else "android"
    target = "com.example.rn"
    if kind == "apk":
        apk = tmp_path / "app-release.apk"
        with ZipFile(apk, "w") as bundle:
            bundle.writestr("assets/index.android.bundle", "console.log('fixture');")
        target = str(apk)
    elif kind == "mac-app":
        from plistlib import dumps

        bundle = tmp_path / "Fixture.app"
        contents = bundle / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(dumps({"CFBundleIdentifier": "com.example.fixture"}))
        target = str(bundle)

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'run.db'}",
        artifacts_dir=tmp_path / "artifacts",
        gemini_api_key="fixture-key",
        llm_intent=True,
    )
    init_db(settings.database_url)
    with session(settings.database_url) as db:
        run = Run(
            project="source-free",
            platform=platform,
            repo_url="",
            live_url="uploaded-release.apk" if kind == "upload" else target,
            status="claimed",
            options={"phase2": True, "modules": ["rotation"], "journeys": True,
                     "llm_intent": True, "load_test": True, "uploaded_apk": kind == "upload",
                     "test_mode": "frontend" if kind == "mac-app" else None},
        )
        db.add(run)
        db.commit()
        run_id = run.id

    if kind == "upload":
        uploaded = apk_upload_path(settings.artifacts_dir, run_id)
        uploaded.parent.mkdir()
        with ZipFile(uploaded, "w") as bundle:
            bundle.writestr("assets/index.android.bundle", "console.log('fixture');")
    app_pipeline.run_app_pipeline(run_id, settings)
    if kind == "upload":
        assert not uploaded.exists()

    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        assert run.status == "completed", (run.error, run.message)
        assert run.stats["code_available"] is False and run.stats["sources"] == []
        assert run.stats["build"]["kind"] == {"installed": "installed", "mac-app": "app"}.get(kind, "apk")
        assert run.stats["screens"] > 0 and run.stats["journeys"]["total"] == 0
        assert run.stats["load_test"]["status"] == "disabled"
        components = db.query(Component).filter_by(run_id=run_id).all()
        assert components and all(c.source == "observed" and not c.code_file for c in components)
        tests = db.query(Test).filter_by(run_id=run_id).all()
        assert any(t.technique == "content" and t.status == "fail" for t in tests)
        if platform == "macos":
            assert any(t.test_key == "desktop|traversal" for t in tests)
            assert run.stats["desktop_coverage"]["controls_observed"] > 0
        assert any(t.technique == "build" for t in tests) == (kind in ("apk", "upload"))
        finding = db.query(Finding).filter(Finding.run_id == run_id, Finding.title.contains("Invalid Date")).one()
        assert finding.category == "functional" and not finding.code_ref
        assert any(evidence["type"] == "screenshot" for evidence in finding.evidence)

    run_dir = settings.artifacts_dir / "runs" / run_id
    html = (run_dir / "report.html").read_text()
    assert "Source coverage unavailable" in html and "Invalid Date" in html
    assert not (run_dir / "journeys.json").exists()
    metrics = generate_latest(build_registry(settings.database_url)).decode()
    assert 'qa_latest_coverage_ratio{project="source-free"}' not in metrics
    assert 'qa_latest_tested_ratio{project="source-free"}' in metrics


@pytest.mark.parametrize("error", [RuntimeError, SystemExit])
def test_android_upload_cleanup_preserves_interrupted_runs(tmp_path, monkeypatch, error):
    from qaplatform.app import pipeline as app_pipeline
    from qaplatform.app.uploads import apk_upload_path
    from qaplatform.config import Settings
    from qaplatform.db.models import Run
    from qaplatform.db.session import init_db, session

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'run.db'}", artifacts_dir=tmp_path / "artifacts")
    init_db(settings.database_url)
    with session(settings.database_url) as db:
        run = Run(platform="android", repo_url="", live_url="release.apk", options={"uploaded_apk": True})
        db.add(run)
        db.commit()
        run_id = run.id
    uploaded = apk_upload_path(settings.artifacts_dir, run_id)
    uploaded.parent.mkdir()
    uploaded.write_bytes(b"fixture")

    def fail_to_resolve(spec, platform):
        assert Path(spec) == uploaded and platform == "android"
        raise error("fixture")

    monkeypatch.setattr(app_pipeline, "resolve_build", fail_to_resolve)
    if error is SystemExit:
        with pytest.raises(SystemExit):
            app_pipeline.run_app_pipeline(run_id, settings)
    else:
        app_pipeline.run_app_pipeline(run_id, settings)

    with session(settings.database_url) as db:
        assert db.get(Run, run_id).status == ("running" if error is SystemExit else "failed")
    assert uploaded.exists() == (error is SystemExit)


def test_native_pipeline_two_sources_journeys_cleanup_and_load(tmp_path, monkeypatch, api_server):
    from qaplatform.app import pipeline as app_pipeline
    from qaplatform.app.build import BuildInfo
    from qaplatform.config import Settings
    from qaplatform.db.models import Component, Run, Test
    from qaplatform.db.session import init_db, session

    app_repo = _write(tmp_path / "mobile", RN_APP)
    be_repo = _write(tmp_path / "backend", BACKEND)
    monkeypatch.setattr(app_pipeline, "AppDevice", PipelineDevice)
    monkeypatch.setattr(app_pipeline, "resolve_build", lambda spec, platform: BuildInfo(platform="android", kind="installed", app_id="com.example.rn", label="RN"))
    for k, v in {"LOAD_TEST_USERS": "2", "LOAD_TEST_MAX_RPS": "10", "LOAD_TEST_DURATION_SECONDS": "5"}.items():
        monkeypatch.setenv(k, v)
    settings = Settings.from_env()
    init_db(settings.database_url)
    with session(settings.database_url) as db:
        run = Run(project="pipe", platform="android", repo_url=str(app_repo), live_url="com.example.rn", status="claimed",
                  options={"max_pages": 10, "phase2": True, "modules": ["rotation"], "journeys": True, "load_test": True, "load_test_base_url": api_server,
                           "codebase_sources": [{"role": "frontend", "source": str(app_repo)}, {"role": "backend", "source": str(be_repo)}]})
        db.add(run)
        db.commit()
        run_id = run.id
    app_pipeline.run_app_pipeline(run_id, settings)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        assert run.status == "completed", (run.error, run.message)
        stats = run.stats
        techniques = {t.technique for t in db.query(Test).filter(Test.run_id == run_id)}
        component_files = {c.code_file for c in db.query(Component).filter(Component.run_id == run_id) if c.code_file}
    backend_files = {str(p.relative_to(be_repo)) for p in be_repo.rglob("*") if p.is_file()}
    assert [s["role"] for s in stats["sources"]] == ["frontend", "backend"] and "API routes" in stats["sources"][1]["note"]
    assert stats["backend"]["routes"] >= 6
    # backend files never count as UI components
    assert not backend_files & component_files and component_files  # backend files never count as UI components
    assert {"journey", "validation", "load"} <= techniques
    assert stats["journeys"]["completed"] >= 1 and stats["qa_data"]["created"] >= 1 and stats["qa_data"]["leftover"] == 0
    assert stats["load_test"]["status"] == "pass" and stats["load_test"]["endpoints"][0]["path"] == "/api/health"
    assert stats["ai"]["used"] is False  # no GEMINI_API_KEY: the plan from the code still ran
    run_dir = settings.artifacts_dir / "runs" / run_id
    html = (run_dir / "report.html").read_text()
    assert "Backend API load test" in html and "Journeys and QA test data" in html and "Backend ·" in html
    assert json.loads((run_dir / "qa_data.json").read_text())["records"] and (run_dir / "journeys.json").exists() and (run_dir / "backend.json").exists()
