import gzip
import json

import httpx
import pytest

from qaplatform.app import assertions
from qaplatform.app.device import Snapshot, parse_android
from qaplatform.app.explore import Screen
from qaplatform.explore.crawler import ObservedElement


def _element(name: str, *, visible: bool = True) -> ObservedElement:
    return ObservedElement(selector=name, name=name, role="button", tag="Button", visible=visible)


def test_observed_ui_assertion_matches_whole_path_and_attaches_evidence():
    home = Screen(sig="home", snap=Snapshot(texts=["Welcome, Tester"]), route="Home")
    report = Screen(sig="report", snap=Snapshot(elements=[_element("Résumé   prêt"), _element("Hidden details", visible=False)]),
                    path=[_element("Reports"), _element("Today")], screenshot="report.png", route="Report")
    screens = [home, report]

    initial = assertions.validate_ui_assertion("", "Welcome")
    assert assertions.check_ui_assertion(screens, initial).status == "pass"
    config = assertions.validate_ui_assertion(" reports \n TODAY  ", "RÉSUMÉ prêt")
    result = assertions.check_ui_assertion(screens, config)
    assert result.status == "pass" and result.route == "Report"
    assert result.evidence == [{"type": "screenshot", "path": "report.png", "viewport": "device"}]
    hidden = assertions.validate_ui_assertion("Reports\nToday", "Hidden details")
    assert assertions.check_ui_assertion(screens, hidden).status == "fail"
    partial = assertions.validate_ui_assertion("Reports", "Résumé prêt")
    assert assertions.check_ui_assertion(screens, partial).status == "blocked"
    assert assertions.validate_ui_assertion("", "") is None
    with pytest.raises(ValueError, match="expected on the app screen"):
        assertions.validate_ui_assertion("Reports", "")


def test_ui_assertion_ignores_undisplayed_android_text():
    snap = parse_android("""<hierarchy>
      <node class="android.widget.TextView" text="Visible title" displayed="true"/>
      <node class="android.widget.Button" displayed="true">
        <node class="android.widget.TextView" text="Hidden details" displayed="false"/>
      </node>
    </hierarchy>""", "com.example.app")
    screens = [Screen(sig="home", snap=snap)]
    visible = assertions.validate_ui_assertion("", "Visible title")
    hidden = assertions.validate_ui_assertion("", "Hidden details")
    assert assertions.check_ui_assertion(screens, visible).status == "pass"
    assert assertions.check_ui_assertion(screens, hidden).status == "fail"


def test_api_assertion_validation_keeps_token_out_of_config():
    assert assertions.validate_api_assertion("https://qa.example", "", "", "", "") is None
    config = assertions.validate_api_assertion("https://qa.example/v1/", "/health", "", "service.ready", "false", "fixture-token")
    assert config == {"base_url": "https://qa.example/v1", "path": "/health", "expected_status": 200,
                      "json_field": "service.ready", "expected_value": False}
    assert assertions.validate_api_assertion("https://qa.example", "/health", "", "status", "ready")["expected_value"] == "ready"
    assert assertions.validate_api_assertion("https://qa.example", "/health", "", "status", '""')["expected_value"] == ""


@pytest.mark.parametrize(("base", "path", "status", "field", "value", "token"), [
    ("ftp://qa.example", "/health", "", "", "", ""),
    ("https://a:b@qa.example", "/health", "", "", "", ""),
    ("https://qa.example", "https://other.example/health", "", "", "", ""),
    ("https://qa.example", "//other.example/health", "", "", "", ""),
    ("https://qa.example", "/%2fother.example/health", "", "", "", ""),
    ("https://qa.example/v1", "/../health", "", "", "", ""),
    ("https://qa.example", "/health?secret=fixture", "", "", "", ""),
    ("https://qa.example", "/health", "99", "", "", ""),
    ("https://qa.example", "/health", "600", "", "", ""),
    ("https://qa.example", "/health", "", "a..b", "null", ""),
    ("https://qa.example", "/health", "", "a.b", "", ""),
    ("https://qa.example", "/health", "", "", "", "bad\nheader"),
])
def test_api_assertion_bad_input(base, path, status, field, value, token):
    with pytest.raises(ValueError):
        assertions.validate_api_assertion(base, path, status, field, value, token)


@pytest.fixture
def mock_api(monkeypatch):
    client = httpx.Client

    def install(respond):
        def client_factory(**kwargs):
            assert kwargs["timeout"] == assertions.API_TIMEOUT_SECONDS
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
            return client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(assertions.httpx, "Client", client_factory)

    return install


def test_api_assertion_uses_one_get_and_keeps_payload_private(mock_api):
    calls = []

    def respond(request):
        calls.append(request)
        body = json.dumps({"service": {"ready": True}, "private": "response-secret"}).encode()
        return httpx.Response(200, stream=httpx.ByteStream(body))

    mock_api(respond)
    config = assertions.validate_api_assertion("https://qa.example/v1", "/health", "200", "service.ready", "true")
    result = assertions.check_api_assertion(config, "fixture-token")
    assert result.status == "pass"
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url) == "https://qa.example/v1/health"
    assert calls[0].headers["Authorization"] == "Bearer fixture-token"
    assert not any(secret in repr(result) for secret in ("fixture-token", "response-secret", "qa.example"))


@pytest.mark.parametrize(("payload", "status", "message"), [
    ({"service": {"ready": 1}}, "fail", "differed"),
    ({"service": {"other": True}}, "fail", "missing"),
    (b"invalid JSON", "fail", "parsing failed"),
    (b"x" * (assertions.MAX_JSON_BYTES + 1), "blocked", "too large"),
])
def test_api_assertion_reports_json_results_without_values(mock_api, payload, status, message):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    mock_api(lambda request: httpx.Response(200, stream=httpx.ByteStream(body)))
    config = assertions.validate_api_assertion("https://qa.example", "/health", "", "service.ready", "true")
    result = assertions.check_api_assertion(config)
    assert result.status == status and message in result.observed
    assert "service.ready" not in repr(result)


def test_api_assertion_does_not_follow_redirects(mock_api):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/"})

    mock_api(respond)
    config = assertions.validate_api_assertion("https://qa.example", "/health", "", "", "")
    result = assertions.check_api_assertion(config)
    assert result.status == "fail" and result.observed == "HTTP 302"
    assert len(calls) == 1


def test_api_json_assertion_blocks_when_server_returns_compression(mock_api):
    def respond(request):
        assert request.headers["Accept-Encoding"] == "identity"
        body = gzip.compress(b'{"ready": true}')
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(body))

    mock_api(respond)
    config = assertions.validate_api_assertion("https://qa.example", "/health", "", "ready", "true")
    result = assertions.check_api_assertion(config)
    assert result.status == "blocked" and "compressed body" in result.observed


def test_api_assertion_blocks_unreachable_endpoint_without_error_details(mock_api):
    def respond(request):
        raise httpx.ConnectError("connection error with private details", request=request)

    mock_api(respond)
    config = assertions.validate_api_assertion("https://qa.example", "/health", "", "", "")
    result = assertions.check_api_assertion(config)
    assert result.status == "blocked"
    assert "private details" not in result.observed
