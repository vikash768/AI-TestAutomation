"""Focused tests for the hardened legacy Robot Framework path."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from robot import run as robot_run

from automation.listeners.prometheus_listener import PrometheusListener
from automation.metrics.publisher import MetricsPublisher
from automation.robot.secure_keywords import RobotSecretInputError, SecureKeywords
from automation.secrets.resolver import SecretResolver


FAKE_SECRET = "ROBOT_SECRET_TEST_VALUE_48291"
PASSWORD_PLACEHOLDER = "{{VALID_USER_PASSWORD}}"


class _RecordingSeleniumLibrary:
    def __init__(self) -> None:
        self.calls = []

    def input_password(self, locator, password, clear=True):
        self.calls.append((locator, password, clear))


class _RecordingPublisher(MetricsPublisher):
    def __init__(self) -> None:
        self.test_results = []
        self.suite_results = []
        self.flush_count = 0

    def publish_test_result(
        self,
        *,
        project,
        suite,
        browser,
        test_type,
        test_id,
        test_name,
        page,
        functionality,
        run_id,
        status,
        duration_seconds,
    ):
        self.test_results.append({
            "project": project,
            "suite": suite,
            "browser": browser,
            "test_type": test_type,
            "test_id": test_id,
            "test_name": test_name,
            "page": page,
            "functionality": functionality,
            "run_id": run_id,
            "status": status,
            "duration_seconds": duration_seconds,
        })

    def publish_suite_result(self, **kwargs):
        self.suite_results.append(kwargs)

    def flush(self):
        self.flush_count += 1


def test_secure_robot_keyword_resolves_placeholder_without_logging(caplog):
    selenium = _RecordingSeleniumLibrary()
    keywords = SecureKeywords(
        resolver=SecretResolver({"VALID_USER_PASSWORD": FAKE_SECRET}),
        selenium_library=selenium,
    )

    assert keywords.value_is_secret_placeholder(PASSWORD_PLACEHOLDER)
    keywords.input_secret_text("id:password", PASSWORD_PLACEHOLDER)

    assert selenium.calls == [("id:password", FAKE_SECRET, True)]
    assert FAKE_SECRET not in caplog.text


def test_secure_robot_keyword_missing_secret_is_controlled():
    keywords = SecureKeywords(
        resolver=SecretResolver({}),
        selenium_library=_RecordingSeleniumLibrary(),
    )

    with pytest.raises(RobotSecretInputError, match="VALID_USER_PASSWORD") as exc:
        keywords.input_secret_text("id:password", PASSWORD_PLACEHOLDER)

    assert FAKE_SECRET not in str(exc.value)


def test_secure_robot_keyword_does_not_echo_driver_exception():
    class EchoingSeleniumLibrary:
        def input_password(self, locator, password, clear=True):
            raise RuntimeError(f"driver rejected {password}")

    keywords = SecureKeywords(
        resolver=SecretResolver({"VALID_USER_PASSWORD": FAKE_SECRET}),
        selenium_library=EchoingSeleniumLibrary(),
    )

    with pytest.raises(RobotSecretInputError) as exc:
        keywords.input_secret_text("id:password", PASSWORD_PLACEHOLDER)

    assert FAKE_SECRET not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_robot_listener_uses_current_metrics_signature_and_stable_run_id(monkeypatch):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    monkeypatch.setenv("PROJECT_NAME", f"project-{FAKE_SECRET}")
    monkeypatch.setenv("BROWSER", f"browser-{FAKE_SECRET}")
    monkeypatch.setenv("TEST_TYPE", f"type-{FAKE_SECRET}")
    publisher = _RecordingPublisher()
    listener = PrometheusListener(publisher=publisher)
    suite = SimpleNamespace(name=f"suite-{FAKE_SECRET}", parent=None)
    result = SimpleNamespace(status="PASS")

    listener.start_suite(suite, None)
    for number in (1, 2):
        test = SimpleNamespace(
            name=f"TC-ROBOT-{number:03d} test-{FAKE_SECRET}"
        )
        listener.start_test(test, result)
        listener.end_test(test, result)

    assert len(publisher.test_results) == 2
    assert {item["run_id"] for item in publisher.test_results} == {listener.run_id}
    assert all(item["page"] == "robot" for item in publisher.test_results)
    assert all(item["functionality"] == "legacy" for item in publisher.test_results)
    assert FAKE_SECRET not in json.dumps(publisher.test_results)


def test_robot_output_does_not_contain_resolved_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    fake_library = tmp_path / "FakeSeleniumLibrary.py"
    fake_library.write_text(
        "class FakeSeleniumLibrary:\n"
        "    ROBOT_LIBRARY_SCOPE = 'GLOBAL'\n"
        "    def input_password(self, locator, password, clear=True):\n"
        "        return None\n",
        encoding="utf-8",
    )
    suite = tmp_path / "secure_input.robot"
    suite.write_text(
        "*** Settings ***\n"
        f"Library    {fake_library}    WITH NAME    SeleniumLibrary\n"
        "Library    automation.robot.secure_keywords.SecureKeywords\n"
        "*** Test Cases ***\n"
        "Secure Placeholder Input\n"
        "    Input Secret Text    id:password    {{VALID_USER_PASSWORD}}\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "robot-output"

    exit_code = robot_run(
        str(suite),
        outputdir=str(output_dir),
        loglevel="TRACE",
        removekeywords="tag:secret",
        console="none",
    )

    assert exit_code == 0
    for output_file in ("output.xml", "log.html", "report.html"):
        content = (output_dir / output_file).read_text(encoding="utf-8")
        assert FAKE_SECRET not in content
