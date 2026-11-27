"""Native app pipeline: code extraction, UI tree parsing, safety, screen checks, routing."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from qaplatform.app.build import resolve_build
from qaplatform.app.checks import screen_checks
from qaplatform.app.code import app_intent, build_app_inventory
from qaplatform.app.device import Snapshot, parse_android, parse_mac
from qaplatform.app.explore import AppExplorer, Screen
from qaplatform.app.pipeline import assign_routes, label_classes
from qaplatform.explore.crawler import ObservedElement

FIX = Path(__file__).resolve().parents[1] / "fixture"

ANDROID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0" width="1080" height="2340">
 <node class="android.widget.FrameLayout" package="com.example" bounds="[0,0][1080,2340]" clickable="false">
  <node class="android.widget.TextView" package="com.example" text="Welcome, undefined" bounds="[40,100][900,160]" clickable="false"/>
  <node class="android.view.ViewGroup" package="com.example" content-desc="Test History" clickable="true" enabled="true" bounds="[40,300][540,600]">
   <node class="android.widget.TextView" package="com.example" text="Test History" bounds="[60,320][500,380]" clickable="false"/>
  </node>
  <node class="android.view.ViewGroup" package="com.example" clickable="true" enabled="true" bounds="[600,300][1040,600]">
   <node class="android.widget.TextView" package="com.example" text="Pending Uploads" bounds="[620,320][1000,380]" clickable="false"/>
  </node>
  <node class="android.widget.ImageButton" package="com.example" clickable="true" enabled="true" bounds="[1000,40][1040,80]"/>
  <node class="android.widget.Button" package="com.example" content-desc="Help" clickable="true" enabled="true" bounds="[1044,40][1074,70]"/>
  <node class="android.widget.EditText" package="com.example" text="" hint="Password" password="false" clickable="true" bounds="[40,700][1040,800]"/>
 </node>
</hierarchy>"""

MAC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication title="Fixture">
 <XCUIElementTypeMenuBar>
  <XCUIElementTypeMenuBarItem title="Apple" x="10" y="0" width="34" height="33">
   <XCUIElementTypeMenu><XCUIElementTypeMenuItem title="Restart…" identifier="_restartRequested:" x="0" y="0" width="200" height="20"/></XCUIElementTypeMenu>
  </XCUIElementTypeMenuBarItem>
  <XCUIElementTypeMenuBarItem title="Edit" x="112" y="0" width="42" height="33">
   <XCUIElementTypeMenu><XCUIElementTypeMenuItem title="Undo" x="0" y="0" width="0" height="0"/></XCUIElementTypeMenu>
  </XCUIElementTypeMenuBarItem>
 </XCUIElementTypeMenuBar>
 <XCUIElementTypeWindow title="Untitled" x="0" y="40" width="1200" height="800">
  <XCUIElementTypeButton label="light-dark(#e6e6e6, #424242)" x="10" y="60" width="46" height="24"/>
  <XCUIElementTypeCheckBox label="Grid" value="1" x="10" y="100" width="13" height="13"/>
 </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


def test_react_native_inventory():
    inv, texts = build_app_inventory(FIX / "rn_app", "android")
    assert inv.framework == "react-native"
    assert {r.path for r in inv.routes} == {"Login", "Home", "TestHistory"}
    assert not next(r for r in inv.routes if r.path == "Login").protected
    by = {(e.route, e.label): e for e in inv.elements}
    # Tile is a component the Home screen renders: its Pressable belongs to Home
    assert ("Home", "Test History") in by or any(e.route == "Home" and e.tag == "Tile" for e in inv.elements)
    tile = next(e for e in inv.elements if e.route == "Home" and e.label == "Test History")
    assert "TestHistory" in tile.navigates_to
    pwd = next(e for e in inv.elements if e.route == "Login" and e.label == "Password")
    assert pwd.kind == "input" and pwd.attrs["secure"] and app_intent(pwd)["expected"][0].startswith("Masks")
    email = next(e for e in inv.elements if e.label == "Email")
    assert email.attrs["id"] == "email-input"
    sign_out = next(e for e in inv.elements if e.route == "Home" and e.label == "Sign out")
    assert "confirm" in sign_out.opens
    clear = next(e for e in inv.elements if e.label == "Clear history")
    assert app_intent(clear)["destructive"]
    row = next(e for e in inv.elements if e.label == "Open result")
    assert row.repeated
    assert "Tests you have run" in texts["TestHistory"]


def test_label_classes_follow_the_code():
    inv, _ = build_app_inventory(FIX / "rn_app", "android")
    intents = {e.key: app_intent(e) for e in inv.elements}
    classes = label_classes(inv, intents)
    assert classes["clear history"] == "destructive"
    assert classes["pending uploads"] == "mutating"  # the handler POSTs, whatever the label says
    assert classes.get("test history", "safe") == "safe"


def test_electron_menus():
    inv, _ = build_app_inventory(FIX / "electron_app", "macos")
    assert inv.framework == "electron"
    items = {(e.route, e.label) for e in inv.elements}
    assert ("menu:Fixture", "About Fixture") in items
    assert ("menu:Fixture", "Zoom In") in items  # declared as a variable, spread into the submenu
    assert ("menu:Fixture", "Quit Fixture") in items
    assert ("menu:Edit", "Select All") in items
    assert not any(e.label == "separator" for e in inv.elements)


def test_parse_android_names_and_selectors():
    snap = parse_android(ANDROID_XML, "com.example")
    names = [(e.role, e.name, e.selector) for e in snap.elements]
    assert ("button", "Test History", "~Test History") in names
    assert any(n == "Pending Uploads" for _, n, _ in names)  # name from the child text
    assert any(r == "textbox" and n == "Password" for r, n, _ in names)
    assert "Welcome, undefined" in snap.texts


def test_parse_mac_skips_apple_menu():
    snap = parse_mac(MAC_XML)
    names = [e.name for e in snap.elements]
    assert "Restart…" not in names and "Apple" not in names
    assert "Edit" in names
    undo = next(e for e in snap.elements if e.name == "Undo")
    assert not undo.visible  # closed menu
    grid = next(e for e in snap.elements if e.name == "Grid")
    assert grid.role == "checkbox" and grid.checked


def test_screen_checks_catch_planted_bugs():
    snap = parse_android(ANDROID_XML, "com.example")
    screen = Screen(sig=snap.signature, snap=snap, route="Home")
    _, findings = screen_checks([screen], density=420, platform="android")
    titles = " | ".join(f.title for f in findings)
    assert "Unformatted value" in titles
    assert "without an accessible name" in titles  # the icon-only ImageButton
    assert "smaller than 24 dp" in titles  # 40 px at 420 dpi is ~15 dp, and the Help button sits 4 px away
    lone = Snapshot(elements=[ObservedElement(selector="~x", role="button", name="Forgot password?", tag="Button", bbox=[500, 500, 233, 41])])
    _, lf = screen_checks([Screen(sig="l", snap=lone, route="Login")], density=320, platform="android")
    assert not any("touch target" in f.title for f in lf)  # 20 dp tall but nothing within 12 dp: WCAG 2.5.8 spacing exception
    assert "Password field 'Password' is not masked" in titles
    mac = parse_mac(MAC_XML)
    _, mf = screen_checks([Screen(sig=mac.signature, snap=mac, route="window")], density=0, platform="macos")
    assert any("accessible name is code" in f.title for f in mf)


class _Dev:
    platform = "android"


def test_classify_holds_back_risky_controls():
    ex = AppExplorer(_Dev(), lambda *a, **k: None, label_class={"pending uploads": "mutating"})

    def cls(name):
        return ex.classify(ObservedElement(selector="~" + name, role="button", name=name, tag="Button"))

    assert cls("Test History") == "safe"
    assert cls("Data sync") == "mutating"
    assert cls("Pending Uploads") == "mutating"
    assert cls("Delete account") == "destructive"
    assert cls("Sign out") == "session_ending"
    assert cls("Capture photo") == "mutating"
    assert cls("Close menu") == "safe"  # a drawer on a phone
    _Dev.platform = "macos"
    try:
        assert cls("Close All") == "destructive"
        assert cls("Close") == "destructive"
        assert cls("Hide draw.io") == "external"
        assert cls("jgraph/drawio") == "external"
        assert cls("Check for updates") == "mutating"
    finally:
        _Dev.platform = "android"


def test_login_fields():
    snap = Snapshot(elements=[
        ObservedElement(selector="~Email", role="textbox", name="Email", tag="EditText"),
        ObservedElement(selector="~Password", role="textbox", name="Password", tag="EditText", type="password"),
        ObservedElement(selector="~Sign in", role="button", name="Sign in", tag="Button"),
    ])
    user, pwd, button = AppExplorer.login_fields(snap)
    assert user.name == "Email" and pwd.name == "Password" and button.name == "Sign in"


def test_assign_routes_by_code_labels():
    inv, texts = build_app_inventory(FIX / "rn_app", "android")
    home = Snapshot(texts=["Welcome"], elements=[ObservedElement(selector="~Test History", role="button", name="Test History", tag="ViewGroup"),
                                                  ObservedElement(selector="~Sign out", role="button", name="Sign out", tag="ViewGroup")])
    other = Snapshot(texts=["Something unrelated"], elements=[])
    screens = [Screen(sig="a", snap=home), Screen(sig="b", snap=other)]
    assign_routes(screens, inv, texts)
    assert screens[0].route == "Home"
    assert screens[1].route.startswith("screen:")


def test_resolve_build_errors():
    with pytest.raises(FileNotFoundError):
        resolve_build("/nope/app.apk", "android")
    with pytest.raises(ValueError):
        resolve_build(str(FIX / "rn_app" / "package.json"), "android")
    info = resolve_build("com.example.app", "android")
    assert info.kind == "installed" and info.app_id == "com.example.app"


def test_api_creates_app_run(tmp_path):
    from fastapi.testclient import TestClient

    from qaplatform.api.app import app
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.worker import claim_next

    client = TestClient(app)
    r = client.post("/runs", data={"platform": "android", "app_path": "com.example.app", "project": "mobile"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[1]
    assert client.get(f"/api/runs/{run_id}").json()["platform"] == "android"
    assert client.post("/runs", data={"platform": "android", "project": "mobile", "repo_url": "x"}).status_code == 400
    assert client.post("/runs", data={"platform": "tv", "repo_url": "x", "app_path": "y"}).status_code == 400
    # a web-only worker never claims it; an app worker does
    import os
    db_url = os.environ["DATABASE_URL"]
    assert claim_next(db_url, ("nothing",)) is None
    claimed = None
    while (c := claim_next(db_url, ("android",))) is not None:
        claimed = c if c[0] == run_id else claimed
    assert claimed == (run_id, "android")
    with session(db_url) as db:
        assert db.get(Run, run_id).status == "claimed"


@pytest.fixture
def submission_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qaplatform.api import app as app_module
    from qaplatform.db.session import init_db

    database_url = f"sqlite:///{tmp_path / 'submissions.db'}"
    init_db(database_url)
    monkeypatch.setattr(app_module.settings, "database_url", database_url)
    monkeypatch.setattr(app_module.settings, "artifacts_dir", tmp_path / "artifacts")
    with TestClient(app_module.app) as client:
        yield client, database_url


@pytest.mark.parametrize(("repo_fields", "target"), [
    ({}, "/tmp/fixture app.apk"),
    ({"repo_url": ""}, "com.example.app"),
    ({"repo_url": " \n\t"}, "/tmp/fixture.apk"),
    ({"repo_url": "https://github.com/example/backend.git", "github_token": "fixture-token", "branch": "develop"}, "/tmp/fixture.apk"),
])
def test_api_accepts_android_without_source(submission_client, repo_fields, target):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.secrets import decrypt_secrets

    client, database_url = submission_client
    response = client.post(
        "/runs",
        data={"platform": "android", "project": " Mobile QA ", "app_path": target, **repo_fields,
              "llm_intent": True, "journeys": True, "remote_qa_data": True,
              "load_test": True, "load_test_base_url": "https://api.example.com",
              "phase2": True, "max_pages": 999},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    with session(database_url) as db:
        run = db.get(Run, run_id)
        assert (run.status, run.platform, run.live_url) == ("queued", "android", target)
        assert run.project == "Mobile QA"
        assert run.repo_url == ""
        assert run.options["test_mode"] == "frontend"
        assert run.options["codebase_sources"] == []
        assert run.options["max_pages"] == settings.max_screens
        assert run.options["branch"] == ""
        assert run.options["llm_intent"] is False
        assert run.options["journeys"] is False
        assert run.options["remote_qa_data"] is False
        assert run.options["load_test"] is False and run.options["load_test_base_url"] == ""
        assert run.options["phase2"] is True
        assert decrypt_secrets(settings.secret_key, run.secrets)["github_token"] == ""


@pytest.mark.parametrize("mode", ["backend", "full"])
def test_api_queues_android_backend_modes(submission_client, mode):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.secrets import decrypt_secrets

    client, database_url = submission_client
    repo = "https://github.com/example/backend.git"
    app = "com.example.app" if mode == "full" else ""
    load_options = {"load_test": True, "load_test_base_url": "https://api.example.com"} if mode == "full" else {}
    response = client.post("/runs", data={
        "platform": "android", "test_mode": mode, "project": "Mobile QA", "repo_url": repo, "github_token": " fixture-token ",
        "app_path": app, "phase2": True, "clean_start": True, "email": "fixture@example.com", "password": "fixture-login",
        "llm_intent": True, "journeys": True, "remote_qa_data": True, **load_options,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    run_response = client.get(f"/api/runs/{run_id}")
    assert run_response.json()["test_mode"] == mode
    assert "fixture-token" not in run_response.text
    with session(database_url) as db:
        run = db.get(Run, run_id)
        assert (run.status, run.platform, run.live_url) == ("queued", "android", app or "Backend source review")
        assert run.repo_url == repo
        assert run.options["codebase_sources"] == [{"role": "backend", "source": repo}]
        assert all(run.options[key] is False for key in ("llm_intent", "journeys", "remote_qa_data"))
        assert run.options["load_test"] == (mode == "full")
        assert run.options["load_test_base_url"] == load_options.get("load_test_base_url", "")
        assert run.options["phase2"] == run.options["clean_start"] == (mode == "full")
        secret = decrypt_secrets(settings.secret_key, run.secrets)
        assert secret["github_token"] == "fixture-token"
        assert secret["email"] == ("fixture@example.com" if mode == "full" else "")
        assert secret["password"] == ("fixture-login" if mode == "full" else "")


@pytest.mark.parametrize(("mode", "target"), [
    ("frontend", "/Applications/Fixture Editor.app"), ("frontend", "com.example.editor"),
    ("backend", ""), ("full", "/tmp/Fixture Editor.dmg"),
])
def test_api_queues_macos_modes(submission_client, mode, target):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.secrets import decrypt_secrets

    client, database_url = submission_client
    repo, token = "https://github.com/example/backend.git", "fixture-per-run-token"
    response = client.post("/runs", data={
        "platform": "macos", "test_mode": mode, "project": "Mac QA", "app_path": target,
        "repo_url": repo, "github_token": token, "clean_start": True, "phase2": True,
        "ui_expected_text": "Editor", "api_base_url": "https://fixture.invalid", "api_path": "/health",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    with session(database_url) as db:
        run = db.get(Run, response.headers["location"].rsplit("/", 1)[-1])
        assert run.platform == "macos"
        assert run.live_url == (target or "Backend source review")
        assert run.options["test_mode"] == mode and run.options["max_pages"] == settings.macos_max_screens
        assert run.options["clean_start"] is False and run.options["phase2"] == (mode != "backend")
        assert run.repo_url == ("" if mode == "frontend" else repo)
        assert run.options["codebase_sources"] == ([] if mode == "frontend" else [{"role": "backend", "source": repo}])
        assert decrypt_secrets(settings.secret_key, run.secrets)["github_token"] == ("" if mode == "frontend" else token)
        assert ("ui" in run.options["assertions"]) == (mode != "backend")
        assert ("api" in run.options["assertions"]) == (mode != "frontend")


@pytest.mark.parametrize(("fields", "message"), [
    ({"test_mode": "frontend", "app_path": "invalid"}, "bundle ID"),
    ({"test_mode": "backend", "app_path": "/Applications/Example.app"}, "Choose Frontend or Full"),
    ({"test_mode": "backend", "repo_url": "https://github.com/example/backend.git"}, "Git token is required"),
    ({"test_mode": "full", "app_path": "/Applications/Example.app"}, "HTTPS backend Git URL"),
    ({"test_mode": "unknown"}, "test mode"),
])
def test_api_validates_macos_scope(submission_client, fields, message):
    client, _ = submission_client
    response = client.post("/runs", data={"platform": "macos", "project": "Mac QA", **fields}, follow_redirects=False)
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_macos_installed_app_route(submission_client, monkeypatch):
    from qaplatform.api import app as app_module

    client, _ = submission_client
    expected = {"available": True, "apps": [{"name": "Editor", "bundle_id": "com.example.editor", "path": "/Applications/Editor.app"}]}
    monkeypatch.setattr(app_module, "read_macos_app_inventory", lambda _: expected)
    response = client.get("/api/macos/apps")
    assert response.json() == expected and response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("overrides", "message"), [
    ({"test_mode": "unknown"}, "test mode"),
    ({"repo_url": ""}, "HTTPS backend Git URL"),
    ({"repo_url": "/local/backend"}, "HTTPS backend Git URL"),
    ({"repo_url": "https://one.invalid/backend.git\nhttps://two.invalid/backend.git"}, "HTTPS backend Git URL"),
    ({"repo_url": "https://user:secret@github.com/example/backend.git"}, "HTTPS backend Git URL"),
    ({"repo_url": "https://github.com/example/backend.git?token=secret"}, "HTTPS backend Git URL"),
    ({"github_token": "  "}, "Git token is required"),
    ({"app_path": "/tmp/fixture.apk"}, "Choose Frontend or Full"),
    ({"test_mode": "full"}, "Provide an APK file"),
    ({"test_mode": "full", "app_path": "/tmp/fixture.apk", "github_token": ""}, "Git token is required"),
    ({"load_test": True}, "backend base URL"),
    ({"load_test": True, "load_test_base_url": "https://user:secret@api.example.com"}, "backend base URL"),
])
def test_api_rejects_invalid_android_backend_inputs(submission_client, overrides, message):
    client, _ = submission_client
    response = client.post("/runs", data={
        "platform": "android", "test_mode": "backend", "project": "Mobile QA", "repo_url": "https://github.com/example/backend.git",
        "github_token": "fixture-token", **overrides,
    }, follow_redirects=False)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert "secret" not in response.text


def test_form_defaults_do_not_prefill_manual_inputs(monkeypatch):
    from qaplatform.api.app import form_defaults

    for key in ("TARGET_REPO_URL", "ANDROID_REPO_URL", "ANDROID_BACKEND_REPO_URL", "IOS_REPO_URL", "DESKTOP_REPO_URL"):
        monkeypatch.setenv(key, "https://git.example.com/env-only.git")
    for key in ("PROJECT_NAME", "ANDROID_PROJECT_NAME", "IOS_PROJECT_NAME", "DESKTOP_PROJECT_NAME"):
        monkeypatch.setenv(key, "env-only project")
    monkeypatch.setenv("ANDROID_APP_PATH", "/tmp/env-only.apk")
    monkeypatch.setenv("DESKTOP_APP_PATH", "/tmp/env-only.app")
    monkeypatch.setenv("APP_BUILD_PATH", "/tmp/general-env-only.apk")
    defaults = form_defaults()
    assert all(defaults[platform]["repo_url"] == "" for platform in ("web", "android", "ios", "macos"))
    assert all(not defaults[platform].get("project") for platform in ("web", "android", "ios", "macos"))
    assert defaults["android"]["app_path"] == ""
    assert defaults["macos"]["app_path"] == ""


@pytest.mark.parametrize("mode", ["frontend", "backend", "full"])
@pytest.mark.parametrize("project", [{}, {"project": " \t "}])
def test_android_requires_a_project_name(submission_client, mode, project):
    client, _ = submission_client
    fields = {**project, "platform": "android", "test_mode": mode}
    if mode != "backend":
        fields["app_path"] = "/tmp/fixture.apk"
    if mode != "frontend":
        fields.update(repo_url="https://github.com/example/backend.git", github_token="fixture-token")
    response = client.post("/runs", data=fields, follow_redirects=False)
    assert response.status_code == 400 and "Project name is required" in response.json()["detail"]


@pytest.mark.parametrize("missing_input", [
    {"platform": "android", "project": "Mobile QA", "app_path": "   "},
    {"platform": "web", "live_url": "https://example.com"},
    {"platform": "ios", "app_path": "example.ipa"},
    {"platform": "macos", "app_path": "com.example.app"},
])
def test_api_rejects_missing_required_inputs(submission_client, missing_input):
    client, _ = submission_client
    response = client.post("/runs", data=missing_input, follow_redirects=False)
    assert response.status_code in (400, 422), response.text


@pytest.fixture
def apk_bytes():
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", "fixture")
    return buffer.getvalue()


@pytest.mark.parametrize("mode", ["frontend", "full"])
def test_api_queues_android_apk_upload(submission_client, tmp_path, apk_bytes, mode):
    from qaplatform.app.uploads import apk_upload_path
    from qaplatform.db.models import Run
    from qaplatform.db.session import session

    client, database_url = submission_client
    repo_fields = {"repo_url": "https://github.com/example/backend.git", "github_token": "fixture-token"} if mode == "full" else {}
    response = client.post("/runs", data={"platform": "android", "test_mode": mode, "project": "Mobile QA", **repo_fields},
                           files={"apk_file": ("release.apk", apk_bytes)}, follow_redirects=False)
    assert response.status_code == 303, response.text
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    uploaded = apk_upload_path(tmp_path / "artifacts", run_id)
    assert uploaded.read_bytes() == apk_bytes
    assert not uploaded.is_relative_to(tmp_path / "artifacts")
    assert client.get(f"/artifacts/{uploaded.parent.name}/{uploaded.name}").status_code == 404
    with session(database_url) as db:
        run = db.get(Run, run_id)
        assert (run.status, run.repo_url, run.live_url) == ("queued", repo_fields.get("repo_url", ""), "release.apk")
        assert run.options["test_mode"] == mode
        assert run.options["uploaded_apk"] is True


@pytest.mark.parametrize(("platform", "path", "filename", "valid_zip", "oversized", "expected"), [
    ("android", "com.example.app", "release.apk", True, False, 400),
    ("ios", "", "release.apk", True, False, 400),
    ("android", "", "release.zip", True, False, 400),
    ("android", "", "release.apk", False, False, 400),
    ("android", "", "release.apk", True, True, 413),
])
def test_api_rejects_invalid_apk_upload(submission_client, tmp_path, monkeypatch, apk_bytes, platform, path, filename, valid_zip, oversized, expected):
    from qaplatform.app import uploads
    from qaplatform.db.models import Run
    from qaplatform.db.session import session

    if oversized:
        monkeypatch.setattr(uploads, "MAX_APK_UPLOAD_BYTES", len(apk_bytes) - 1)
    client, database_url = submission_client
    response = client.post("/runs", data={"platform": platform, "project": "Mobile QA", "app_path": path},
                           files={"apk_file": (filename, apk_bytes if valid_zip else b"invalid")}, follow_redirects=False)
    assert response.status_code == expected, response.text
    assert not list((tmp_path / ".artifacts-uploads").glob("*.apk"))
    with session(database_url) as db:
        assert db.query(Run).count() == 0


def test_findings_say_where_and_how_to_fix():
    from qaplatform.app.build import BuildInfo
    from qaplatform.app.checks import code_checks
    from qaplatform.app.remedy import explain
    from qaplatform.execute.smoke import FindingRecord

    inv, _ = build_app_inventory(FIX / "rn_app", "android")
    tap = FindingRecord(fingerprint="a", route="Home", category="functional", severity="medium", title="Tapping 'Test History' does nothing")
    mask = FindingRecord(fingerprint="b", route="Login", category="security", severity="high", title="Password field 'Password' is not masked")
    build = FindingRecord(fingerprint="c", route="app build", category="security", severity="high", title="APK is signed with the Android debug key", fix_in="app build",
                          fix="use the release key", code_ref="android/app/build.gradle", viewport="build")
    home = Screen(sig="h", snap=Snapshot(), route="Home", path=[ObservedElement(selector="~Open", role="button", name="Open", tag="Button")])
    explain([tap, mask, build], inv=inv, components=[], screens=[home], build=BuildInfo(platform="android", kind="installed", app_id="com.example"), signed_in=True, repo=FIX / "rn_app")
    assert tap.fix_in == "app code" and "onPress" in tap.fix and tap.code_ref.startswith("src/") and ":" in tap.code_ref
    assert "Sign in with the test account" in tap.steps and "Tap 'Open'" in tap.steps and "Tap 'Test History'" in tap.steps
    assert "secureTextEntry" in mask.fix and mask.code_ref.split(":")[0].endswith((".js", ".jsx", ".ts", ".tsx"))
    assert build.fix == "use the release key" and build.code_ref == ""  # the fixture repo has no build.gradle: never point at a missing file

    inv.elements[0].navigates_to = ["Nowhere"]
    tests, findings = code_checks(inv)
    assert tests[0].status == "fail" and "Nowhere" in findings[0].title and findings[0].code_ref == f"{inv.elements[0].file}:{inv.elements[0].line}"


def test_heartbeat_tells_live_workers(tmp_path):
    import json
    import time

    from qaplatform.heartbeat import heartbeat_dir, live_platforms

    d = heartbeat_dir(tmp_path)
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"platforms": ["web"], "ts": time.time()}))
    (d / "b.json").write_text(json.dumps({"platforms": ["android"], "ts": time.time() - 120}))
    assert live_platforms(tmp_path) == {"web"}


def test_interrupted_runs_go_back_to_the_queue(tmp_path):
    import os

    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.worker import requeue_interrupted

    db_url = os.environ["DATABASE_URL"]
    with session(db_url) as db:
        db.add(Run(id="stale0000001", project="p", repo_url="r", live_url="com.x", platform="android", status="running", stage="explore", progress=40))
        db.add(Run(id="stale0000002", project="p", repo_url="r", live_url="https://x", platform="web", status="running", stage="explore", progress=40))
        db.commit()
    assert "stale0000001" in requeue_interrupted(db_url, ("android",), tmp_path)
    with session(db_url) as db:
        assert db.get(Run, "stale0000001").status == "queued" and db.get(Run, "stale0000002").status == "running"  # another platform's worker owns that one
        db.delete(db.get(Run, "stale0000001"))
        db.delete(db.get(Run, "stale0000002"))
        db.commit()
