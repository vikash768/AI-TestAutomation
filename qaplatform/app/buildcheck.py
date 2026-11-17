"""Checks on the build file itself (no device): manifest flags, signing, exported
components, secrets and cleartext URLs shipped in the JS bundle.

Findings here are fixed in the build configuration (AndroidManifest.xml,
build.gradle, signing config) or by removing the value from the code, not by
changing a screen.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from qaplatform.app.build import BuildInfo, _aapt2
from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint

PLAY_TARGET_SDK = 35  # Google Play: new apps and updates must target API 35 (from 31 Aug 2025)

SECRET_PATTERNS = {
    "private key": (rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "critical"),
    "AWS access key": (rb"\bAKIA[0-9A-Z]{16}\b", "critical"),
    "GitHub token": (rb"\b(?:gh[pousr]_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{40,})", "critical"),
    "Stripe secret key": (rb"\bsk_live_[0-9a-zA-Z]{20,}", "critical"),
    "Slack token": (rb"\bxox[baprs]-[0-9A-Za-z-]{10,}", "high"),
    "Google API key": (rb"\bAIza[0-9A-Za-z_\-]{35}\b", "medium"),
    "JSON Web Token": (rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "high"),
}
CLEARTEXT_URL = re.compile(rb"http://(?!localhost|127\.0\.0\.1|10\.0\.2\.2|schemas\.android\.com|www\.w3\.org|ns\.adobe\.com|xml\.org|java\.sun\.com|reactnative\.dev)[a-z0-9.\-]+\.[a-z]{2,}[/A-Za-z0-9._\-]*")
BUNDLE_NAMES = ("assets/index.android.bundle", "assets/index.bundle", "main.jsbundle")


def _manifest(apk: Path) -> list[dict]:
    """AndroidManifest.xml as a flat list of elements: {tag, depth, attrs, parent}."""
    tool = _aapt2()
    if not tool:
        return []
    out = subprocess.run([tool, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)], capture_output=True, text=True, timeout=60).stdout
    elements: list[dict] = []
    stack: list[dict] = []
    for line in out.splitlines():
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("E: "):
            el = {"tag": body[3:].split(" ", 1)[0], "depth": indent, "attrs": {}, "children": []}
            while stack and stack[-1]["depth"] >= indent:
                stack.pop()
            if stack:
                stack[-1]["children"].append(el)
            stack.append(el)
            elements.append(el)
        elif body.startswith("A: ") and stack:
            m = re.match(r"A: (?:http://schemas\.android\.com/apk/res/android:)?([\w:]+)(?:\(0x[0-9a-f]+\))?=(.*)", body)
            if m:
                val = m.group(2)
                raw = re.search(r'\(Raw: "(.*)"\)', val)
                val = raw.group(1) if raw else val.strip('"')
                stack[-1]["attrs"][m.group(1)] = val
    return elements


def _is_true(val: str | None) -> bool:
    return (val or "").lower() in ("true", "0xffffffff", "-1")


def _launcher(el: dict) -> bool:
    def walk(e):
        yield e
        for c in e["children"]:
            yield from walk(c)
    names = {d["attrs"].get("name", "") for d in walk(el)}
    return "android.intent.action.MAIN" in names and "android.intent.category.LAUNCHER" in names


def _apksigner() -> tuple[str | None, dict]:
    """apksigner from the same build-tools as aapt2, and an environment with a Java runtime (Android Studio's own when none is installed)."""
    tool = _aapt2()
    cand = Path(tool).with_name("apksigner") if tool else None
    if cand is None or not cand.exists():
        return None, {}
    env = dict(os.environ)
    if env.get("JAVA_HOME"):
        has_java = True
    elif Path("/usr/libexec/java_home").exists():  # macOS ships a /usr/bin/java stub even without a runtime
        has_java = subprocess.run(["/usr/libexec/java_home"], capture_output=True).returncode == 0
    else:
        has_java = bool(shutil.which("java"))
    if not has_java:
        for jbr in ("/Applications/Android Studio.app/Contents/jbr/Contents/Home", str(Path.home() / "Applications/Android Studio.app/Contents/jbr/Contents/Home")):
            if Path(jbr).is_dir():
                env["JAVA_HOME"] = jbr
                break
        else:
            return None, {}
    return str(cand), env


def build_checks(build: BuildInfo, route: str = "app build") -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    if build.platform != "android" or build.kind != "apk" or not build.path:
        return tests, findings
    apk = Path(build.path)

    def test(key: str, name: str, expected: str, observed: str, ok: bool, category: str = "security") -> str:
        tkey = f"build|{key}"
        tests.append(TestRecord(test_key=tkey, name=name, technique="build", category=category, route=route, expected=expected, observed=observed, status="pass" if ok else "fail"))
        return tkey

    def finding(key: str, severity: str, title: str, expected: str, observed: str, standard: str, fix: str, steps: list[str], tkey: str, review: bool = False, category: str = "security",
                ref: str = "android/app/src/main/AndroidManifest.xml"):
        findings.append(FindingRecord(fingerprint=fingerprint(route, category, title, key), route=route, category=category, severity=severity, title=title, expected=expected,
                                      observed=observed, standard_ref=standard, viewport="build", confidence=0.6 if review else 0.95, reproduced_n=1, reproduced_m=1,
                                      status="needs_review" if review else "reported", test_key=tkey, fix_in="app build", fix=fix, code_ref=ref,
                                      steps=steps))

    dump = f"aapt2 dump xmltree --file AndroidManifest.xml {apk.name}"
    els = _manifest(apk)
    app = next((e for e in els if e["tag"] == "application"), None)
    sdk = next((e for e in els if e["tag"] == "uses-sdk"), None)
    if app is not None:
        a = app["attrs"]
        debuggable = _is_true(a.get("debuggable"))
        tk = test("debuggable", "Release build is not debuggable", "android:debuggable absent or false", f"debuggable={a.get('debuggable', 'absent')}", not debuggable)
        if debuggable:
            finding("debuggable", "critical", "Build is debuggable", "A release build is not debuggable", "android:debuggable=true in the manifest", "OWASP MASVS-RESILIENCE-4",
                    "Build the release variant (./gradlew assembleRelease / bundleRelease): debuggable is false there by default. Remove any android:debuggable=\"true\" from AndroidManifest.xml and debuggable true from the release buildType in android/app/build.gradle.",
                    [f"Run: {dump}", "Look for android:debuggable on <application>"], tk)
        backup = a.get("allowBackup")
        tk = test("backup", "App data is excluded from backups", "android:allowBackup=false (or dataExtractionRules excluding sensitive data)", f"allowBackup={backup or 'absent (defaults to true)'}",
                  backup is not None and not _is_true(backup))
        if backup is None or _is_true(backup):
            finding("backup", "medium", "App data can be copied out through backups", "Sensitive app data (tokens, offline results) is not backed up", f"android:allowBackup={backup or 'absent, which means true'}",
                    "OWASP MASVS-STORAGE-2", "Set android:allowBackup=\"false\" on <application> in android/app/src/main/AndroidManifest.xml, or add android:dataExtractionRules / fullBackupContent that exclude the database and keychain files.",
                    [f"Run: {dump}", "Look for android:allowBackup on <application>"], tk)
        target = int(re.sub(r"\D", "", (sdk or {}).get("attrs", {}).get("targetSdkVersion", "0") or "0") or 0) if sdk else 0
        cleartext = a.get("usesCleartextTraffic")
        cleartext_on = _is_true(cleartext) or (cleartext is None and 0 < target < 28 and "networkSecurityConfig" not in a)
        tk = test("cleartext", "Cleartext HTTP is blocked", "usesCleartextTraffic=false, or a network security config that forbids it",
                  f"usesCleartextTraffic={cleartext or 'absent'}; networkSecurityConfig={'present' if 'networkSecurityConfig' in a else 'absent'}; targetSdk={target or '?'}", not cleartext_on)
        if cleartext_on:
            finding("cleartext", "medium", "App allows unencrypted HTTP traffic", "All traffic uses HTTPS", f"usesCleartextTraffic={cleartext or 'default for targetSdk < 28'}", "OWASP MASVS-NETWORK-1",
                    "Set android:usesCleartextTraffic=\"false\" on <application>, or a res/xml/network_security_config.xml with cleartextTrafficPermitted=\"false\"; serve every endpoint over HTTPS.",
                    [f"Run: {dump}", "Look for android:usesCleartextTraffic on <application>"], tk)
        if target:
            tk = test("target-sdk", f"Targets a current Android API level (>= {PLAY_TARGET_SDK})", f"targetSdkVersion >= {PLAY_TARGET_SDK}", f"targetSdkVersion={target}", target >= PLAY_TARGET_SDK, category="platform")
            if target < PLAY_TARGET_SDK:
                finding("target-sdk", "medium", f"Target SDK {target} is below the Google Play requirement ({PLAY_TARGET_SDK})", f"targetSdkVersion >= {PLAY_TARGET_SDK}", f"targetSdkVersion={target}",
                        "Google Play target API level requirement", f"Raise targetSdkVersion to {PLAY_TARGET_SDK} in android/build.gradle (ext.targetSdkVersion) and retest the behaviour changes of each API level in between.",
                        [f"Run: {dump}", "Read targetSdkVersion on <uses-sdk>"], tk, category="platform", ref="android/build.gradle")
        exposed = []
        for el in els:
            if el["tag"] not in ("activity", "activity-alias", "service", "receiver", "provider"):
                continue
            at = el["attrs"]
            if _is_true(at.get("exported")) and not at.get("permission") and not _launcher(el) and not at.get("name", "").startswith(("androidx.", "com.google.")):
                exposed.append(f"{el['tag']} {at.get('name', '?')}")
        tk = test("exported", "Exported components are protected", "Only the launcher activity is exported without a permission", "; ".join(exposed) or "none besides the launcher", not exposed)
        if exposed:
            finding("exported", "medium", f"{len(exposed)} component(s) exported without a permission", "Components other apps can start require a permission",
                    "; ".join(exposed)[:600], "OWASP MASVS-PLATFORM-1",
                    "Set android:exported=\"false\" on each listed component unless another app must start it; otherwise add android:permission with a signature-level permission.",
                    [f"Run: {dump}", "Find <activity>/<service>/<receiver>/<provider> with android:exported=true"], tk, review=True)

    signer, signer_env = _apksigner()
    if not signer:
        tests.append(TestRecord(test_key="build|signing", name="Signed with a release key", technique="build", category="security", route=route,
                                expected="Release certificate (not the Android debug key)", observed="not checked: apksigner or a Java runtime is missing on this machine", status="blocked"))
    else:
        out = subprocess.run([signer, "verify", "--print-certs", str(apk)], capture_output=True, text=True, timeout=60, env=signer_env)
        dn = next((l.split(":", 1)[1].strip() for l in out.stdout.splitlines() if "certificate DN" in l), "")
        debug_key = "CN=Android Debug" in dn
        tk = test("signing", "Signed with a release key", "Release certificate (not the Android debug key)", dn or (out.stderr.strip()[:200] or "unsigned"), bool(dn) and not debug_key)
        if debug_key:
            finding("signing", "high", "APK is signed with the Android debug key", "Release builds are signed with the upload/release key", dn, "OWASP MASVS-RESILIENCE-2",
                     "Configure signingConfigs.release in android/app/build.gradle with the release keystore (from a CI secret, not the repo) and use it in buildTypes.release; React Native's template signs release with the debug key until this is changed.",
                     [f"Run: apksigner verify --print-certs {apk.name}", "Read 'certificate DN'"], tk, ref="android/app/build.gradle")

    try:
        with zipfile.ZipFile(apk) as z:
            names = [n for n in z.namelist() if n in BUNDLE_NAMES]
            blob = b"".join(z.read(n) for n in names)
    except Exception:
        names, blob = [], b""
    if names:
        hits = []
        for label, (pat, severity) in SECRET_PATTERNS.items():
            found = sorted(set(re.findall(pat, blob)))
            if found:
                hits.append((label, severity, found))
        tk = test("secrets", "No secrets in the JS bundle", "No private keys, access tokens or API secrets in the shipped bundle",
                  "; ".join(f"{label} x{len(f)}" for label, _, f in hits) or f"none in {', '.join(names)}", not hits)
        for label, severity, found in hits:
            sample = found[0].decode("ascii", "replace")
            masked = sample[:8] + "…" + sample[-4:] if len(sample) > 16 else sample[:6] + "…"
            finding(f"secret-{label}", severity, f"{label} shipped in the app bundle", "Secrets stay on the server; the app holds only per-user tokens it receives at sign-in",
                    f"{len(found)} value(s) matching a {label} in {names[0]}, e.g. {masked}", "OWASP MASVS-STORAGE-1 / CWE-798",
                    f"Search the codebase for the value (git grep '{sample[:8]}'), revoke and rotate it, and move the call that needs it behind your backend. Anything in the bundle can be read by anyone with the APK.",
                    [f"unzip -p {apk.name} {names[0]} | strings | grep '{sample[:8]}'"], tk, review=label == "Google API key", ref="")
        urls = sorted({u.decode() for u in CLEARTEXT_URL.findall(blob)})
        tk = test("http-urls", "No plain-HTTP endpoints in the bundle", "Every URL the app calls is https://", ", ".join(urls[:8]) or "none", not urls)
        if urls:
            finding("http-urls", "low", f"{len(urls)} plain-HTTP URL(s) in the app bundle", "Endpoints use https://", ", ".join(urls[:10]), "OWASP MASVS-NETWORK-1",
                    "Change these URLs to https:// in the code (search the repo for each one); if one is only documentation text, ignore this finding.",
                    [f"unzip -p {apk.name} {names[0]} | strings | grep 'http://'"], tk, review=True, ref="")
    return tests, findings
