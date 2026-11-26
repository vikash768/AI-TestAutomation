"""Evaluate user-specified expectations on observed screens and an explicit GET endpoint."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from urllib.parse import unquote, urlsplit

import httpx

from qaplatform.app.explore import Screen
from qaplatform.execute.smoke import TestRecord

API_TIMEOUT_SECONDS = 5.0
MAX_JSON_BYTES = 1024 * 1024
INVALID_URL_CHARS = re.compile(r"[\s\\?#\x00-\x1f\x7f]")
INVALID_DECODED_PATH_CHARS = re.compile(r"[\\\x00-\x1f\x7f]")


def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def validate_ui_assertion(path: str, expected_text: str) -> dict | None:
    labels = [label.strip() for label in path.splitlines() if label.strip()]
    expected_text = expected_text.strip()
    if not labels and not expected_text:
        return None
    if not expected_text:
        raise ValueError("Enter the text expected on the app screen")
    return {"path": labels, "expected_text": expected_text}


def _is_endpoint_path(path: str) -> bool:
    decoded = unquote(path)
    return (path.startswith("/") and not decoded.startswith("//")
            and not INVALID_URL_CHARS.search(path) and not INVALID_DECODED_PATH_CHARS.search(decoded)
            and not any(segment in (".", "..") for segment in decoded.split("/")))


def validate_api_assertion(base_url: str, path: str, expected_status: str, json_field: str, expected_value: str,
                           token: str = "") -> dict | None:
    """Accept nested object keys and an expected JSON literal or plain string."""
    base_url, path, expected_status, json_field, expected_value, token = (
        value.strip() for value in (base_url, path, expected_status, json_field, expected_value, token)
    )
    if not any((path, expected_status, json_field, expected_value, token)):
        return None  # a base URL can be supplied for the separate performance check
    if not _is_endpoint_path(path):
        raise ValueError("Enter an API endpoint path beginning with a single slash, such as /health")
    try:
        base = urlsplit(base_url)
        port = base.port
    except ValueError:
        raise ValueError("Enter a valid HTTP or HTTPS backend base URL") from None
    if (base.scheme not in ("http", "https") or not base.hostname or base.username is not None or port == 0
            or INVALID_URL_CHARS.search(base_url) or (base.path and not _is_endpoint_path(base.path))):
        raise ValueError("Enter a valid HTTP or HTTPS backend base URL")
    expected_status = expected_status or "200"
    if not re.fullmatch(r"[1-5][0-9]{2}", expected_status):
        raise ValueError("Enter an expected HTTP status between 100 and 599")
    if token and not re.fullmatch(r"[!-~]+", token):
        raise ValueError("Enter the bearer token without spaces or line breaks")
    if bool(json_field) != bool(expected_value) or (json_field and any(not part.strip() for part in json_field.split("."))):
        raise ValueError('Enter both a dotted JSON field and its expected value. Use "" for an empty string')
    parsed_value = None
    if json_field:
        try:
            parsed_value = json.loads(expected_value)
        except json.JSONDecodeError:
            parsed_value = expected_value
        except (ValueError, RecursionError):
            raise ValueError("Enter a valid JSON value or plain text") from None
        try:
            json.dumps(parsed_value, allow_nan=False)
        except (ValueError, RecursionError):
            raise ValueError("Enter a finite JSON value or quote the text you expect") from None
    return {"base_url": base_url.rstrip("/"), "path": path, "expected_status": int(expected_status),
            "json_field": json_field, "expected_value": parsed_value}


def check_ui_assertion(screens: list[Screen], config: dict) -> TestRecord:
    labels = config["path"]
    expected = config["expected_text"]
    normalized_expected = _normalize_text(expected)
    normalized_path = [_normalize_text(label) for label in labels]
    steps = [{"action": "tap", "target": label} for label in labels] or [{"text": "Inspect the initial screen"}]
    result = TestRecord(test_key="assertion|ui", name="Expected text on app screen", technique="ui_assertion",
                        category="functional", route="", steps=steps, expected=f"Visible screen text contains {expected!r}",
                        observed="The configured screen was not reached during exploration", status="blocked")
    for screen in screens:
        if [_normalize_text(element.name) for element in screen.path] != normalized_path:
            continue
        text = [*screen.snap.texts, *(element.name for element in screen.snap.elements if element.visible and element.type != "password")]
        present = any(normalized_expected in _normalize_text(item) for item in text)
        result.route = screen.route
        result.observed = "Expected text found" if present else "Expected text was absent from the observed screen"
        result.status = "pass" if present else "fail"
        if screen.screenshot:
            result.evidence = [{"type": "screenshot", "path": screen.screenshot, "viewport": "device"}]
        break
    return result


def _check_json(response: httpx.Response, config: dict, result: TestRecord, deadline: float) -> None:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        result.observed += ". The server returned a compressed body. JSON verification requires an uncompressed response"
        return
    body = bytearray()
    for chunk in response.iter_raw():
        if time.monotonic() > deadline:
            result.observed += ". Timed out reading the JSON body"
            return
        if len(body) + len(chunk) > MAX_JSON_BYTES:
            result.observed += ". JSON body was too large to check"
            return
        body.extend(chunk)
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        result.status = "fail"
        result.observed += ". JSON parsing failed"
        return
    for key in config["json_field"].split("."):
        if not isinstance(value, dict) or key not in value:
            result.status = "fail"
            result.observed += ". The configured JSON field was missing"
            return
        value = value[key]
    matched = json.dumps(value, sort_keys=True) == json.dumps(config["expected_value"], sort_keys=True)
    result.status = "pass" if matched else "fail"
    result.observed += ". The JSON value matched" if matched else ". The JSON value differed from the expectation"


def check_api_assertion(config: dict, token: str = "") -> TestRecord:
    status = config["expected_status"]
    field = config["json_field"]
    expected = f"HTTP {status}" + (" and the configured JSON value" if field else "")
    result = TestRecord(test_key="assertion|api", name="Expected backend API response", technique="api_assertion",
                        category="functional", route="backend", steps=[{"text": "GET the configured endpoint"}],
                        expected=expected, observed="Could not complete the GET request", status="blocked")
    headers = {"Accept-Encoding": "identity"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    start = time.monotonic()
    try:
        with httpx.Client(timeout=API_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", config["base_url"] + config["path"], headers=headers) as response:
                result.observed = f"HTTP {response.status_code}"
                if response.status_code != status:
                    result.status = "fail"
                elif field:
                    _check_json(response, config, result, start + API_TIMEOUT_SECONDS)
                else:
                    result.status = "pass"
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        result.status = "blocked"
        result.observed = "Could not complete the GET request"
    result.duration_ms = round((time.monotonic() - start) * 1000)
    return result
