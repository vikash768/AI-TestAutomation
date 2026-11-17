"""Makes each app finding actionable for the developer who has to fix it.

Fills four fields the checks leave empty:
  fix_in    which part to change: app code | app build | backend | content
  fix       what to change, in the app's own terms (React Native / Android)
  code_ref  path:line in the repo (the control's declaration, else the screen file)
  steps     how to see it happen on a device, from launch
"""
from __future__ import annotations

import re
from pathlib import Path

from qaplatform.app.build import BuildInfo
from qaplatform.app.explore import Screen
from qaplatform.execute.smoke import ComponentRecord, FindingRecord
from qaplatform.ingest.js_parser import CodeInventory
from qaplatform.reconcile.matcher import normalize

QUOTED = re.compile(r"'([^']{1,80})'")

# (title pattern, fix_in, how to fix, the action that shows the bug); {name} is the quoted control, {route} the screen
RULES: list[tuple[str, str, str, str]] = [
    (r"^Screen renders blank", "app code",
     "The screen renders nothing. Render a loading indicator while its data loads, an empty state when the list is empty, and an error with Retry when the request fails; never return null from the screen component.",
     "Look at the screen"),
    (r"^Unformatted value shown", "app code",
     "A missing or non-numeric value is rendered as-is. Guard it where the text is built: `{value ?? '—'}` for missing data, `Number.isFinite(n) ? n.toFixed(1) : '—'` for numbers, and never interpolate an object into text.",
     "Read the text on the screen"),
    (r"without an accessible name", "app code",
     "Icon-only controls have no name, so TalkBack / VoiceOver announce just 'button'. Add accessibilityLabel (and accessibilityRole=\"button\") to each Pressable / TouchableOpacity that has no visible text, e.g. <Pressable accessibilityLabel=\"Open menu\" accessibilityRole=\"button\">.",
     "Turn on TalkBack and swipe through the controls (the unnamed ones are listed under Actual)"),
    (r"accessible name is code", "app code",
     "A raw value (CSS, an id, a template) is passed as the control's label. Pass a human-readable string to accessibilityLabel / title instead.",
     "Turn on TalkBack and focus the control"),
    (r"touch target", "app code",
     "The control is smaller than 24 x 24 dp (48 dp is the Android guideline). Give it minWidth/minHeight: 48 in its style, or keep the visual size and add hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }} to the Pressable.",
     "Try to tap the small controls listed under Actual"),
    (r"overlapping controls", "app code",
     "Two controls cover each other, so one tap can hit the wrong one. Check absolute positioning and negative margins in the screen's styles; lay the controls out with flex and gap instead.",
     "Look at the overlapping controls listed under Actual"),
    (r"extend past the screen edge|cut off in landscape", "app code",
     "Fixed widths push controls off the screen. Use flexShrink: 1 / flexWrap: 'wrap', maxWidth: '100%', or wrap the content in a ScrollView; size from useWindowDimensions() rather than constants.",
     "Look at the right edge of the screen"),
    (r"[Pp]assword field.*not masked", "app code",
     "The password TextInput shows what is typed. Set secureTextEntry on it (toggle it with a show/hide button if needed).",
     "Type into the password field"),
    (r"^App (crashes|stops responding) when '", "app code",
     "The handler of '{name}' throws (crash) or blocks the main thread (ANR). The stack trace is under Actual and in the device log. Wrap the handler in try/catch and show an error; move heavy work off the UI thread (InteractionManager.runAfterInteractions, a worker, or native async).",
     "Tap '{name}'"),
    (r"^Fatal or JavaScript error logged when '", "app code",
     "Tapping '{name}' logs an uncaught error (shown under Actual). Catch the rejected promise or thrown error in the onPress handler and show the user what went wrong.",
     "Tap '{name}' and watch `adb logcat -s ReactNativeJS`"),
    (r"^Tapping '.*' does nothing", "app code",
     "'{name}' looks enabled but nothing happens when it is tapped: no navigation, no dialog, no message. Wire its onPress to the intended action, or disable it (disabled + accessibilityState={{ disabled: true }}) while it cannot act, or show a message saying why.",
     "Tap '{name}'"),
    (r"does not navigate to", "app code",
     "The code's handler for '{name}' navigates, but the screen did not change on the device. Check that the navigation call is reached (a guard or early return before it), that the target is registered in the navigator, and that no error is swallowed.",
     "Tap '{name}'"),
    (r"^Cold start takes", "app code",
     "Startup is slow. Defer work done before the first render (storage reads, SDK init, large imports): lazy-load screens, keep Hermes on, and move non-critical init after the first frame. Measure with `adb shell am start -W`.",
     "Force-stop the app, then launch it: adb shell am start -W -n {pkg}/{activity}"),
    (r"crashes on rotation", "app code",
     "The app crashes when the activity is recreated. Check the stack trace; keep android:configChanges=\"keyboard|keyboardHidden|orientation|screenSize|uiMode\" on MainActivity (React Native template) or restore state on recreation.",
     "Rotate the device to landscape"),
    (r"state lost after rotation", "app build",
     "Rotating rebuilds the activity and the app restarts its navigation. Add android:configChanges=\"keyboard|keyboardHidden|orientation|screenSize|uiMode\" to MainActivity in AndroidManifest.xml, or persist the navigation state.",
     "Rotate the device to landscape and back"),
    (r"crashes when resumed", "app code",
     "The app crashes when it comes back from the background. Check AppState 'change' listeners and timers that run on resume (stack trace under Actual).",
     "Press Home, wait 5 s, open the app again"),
    (r"does not resume where", "app code",
     "The app forgets the screen the user was on when it is backgrounded. Check AppState listeners that reset navigation or sign the user out on resume; if that is an intended lock screen, mark this finding 'not a bug'.",
     "Press Home, wait 5 s, open the app again"),
    (r"Session is lost when the app is relaunched", "app code",
     "The user is signed out after the app restarts. Persist the session (token in react-native-keychain / EncryptedStorage) when the user signs in (and 'Remember me' is ticked), and restore it on startup before choosing the first screen.",
     "Force-stop the app (adb shell am force-stop {pkg}) and open it again"),
    (r"crashes opening .* offline|is blank when offline|Unhandled error opening .* offline", "app code",
     "The screen fails without a network. Catch fetch failures (TypeError: Network request failed), show cached data or an offline message with Retry, and check NetInfo before loading.",
     "Turn on airplane mode, then open {route}"),
    (r"on long input in '", "app code",
     "A very long value breaks the field. Set maxLength on the TextInput and validate length before using the value.",
     "Type 300 characters into '{name}'"),
    (r"^Sign-in accepted", "backend",
     "Invalid credentials were accepted. The server must answer 401 for a wrong or empty password, and the app must only navigate after the API returns a session.",
     "On the sign-in screen enter the invalid credentials and tap Sign In"),
    (r"^No feedback after sign-in", "app code",
     "A failed sign-in shows no message. Show the server's error (or 'Incorrect username or password') next to the form.",
     "On the sign-in screen submit the empty / wrong credentials"),
    (r"reveals whether an account exists", "backend",
     "Different messages for an unknown account and a wrong password let anyone find valid usernames. Return one generic message ('Incorrect username or password') for both.",
     "Sign in with an unknown username, then with a known one and a wrong password; compare the messages"),
    (r"^Error logged on sign-in", "app code",
     "A failed sign-in throws in the app (log line under Actual). Catch it in the sign-in handler and show a message.",
     "Submit wrong credentials and watch `adb logcat -s ReactNativeJS`"),
    (r"^Slow or janky rendering", "app code",
     "Many frames take longer than 16 ms. Profile with the React Native perf monitor / Android Studio profiler; use FlatList (not ScrollView + map) for lists, memoize rows, and avoid heavy work in render.",
     "Scroll and navigate through the app; adb shell dumpsys gfxinfo {pkg}"),
    (r"^High memory use", "app code",
     "The app uses a lot of memory. Check for large images loaded at full size, lists that keep every row mounted, and caches that never shrink.",
     "Use the app, then: adb shell dumpsys meminfo {pkg}"),
    (r"^Backend errors under load", "backend",
     "The endpoint fails when several users call it at once. Check the server log for the failing requests (status codes are under Actual): exhausted DB connection pool, timeouts to a dependency, rate limiting that answers 5xx instead of 429, or unhandled errors. Size the pool for the concurrency, add timeouts and return 429 with Retry-After when throttling.",
     "Send the same concurrent GET requests (see the test's Steps) and watch the server log"),
    (r"^Slow backend under load", "backend",
     "The endpoint's p95 latency is above the limit under load. Profile the handler: add the missing index or pagination, cache the response, avoid N+1 queries, and check the DB pool size.",
     "Send the same concurrent GET requests (see the test's Steps) and measure p95"),
    (r"^Invalid input accepted", "app code",
     "The form submits a value its own rules should refuse. Validate every field before submit (the same rule the API applies) and show the message next to the field; disable the submit control while the form is invalid.",
     "Fill the form with the value from the Steps and tap '{name}'"),
    (r"accepts more than \d+ characters", "app code",
     "The field takes more characters than allowed. Set maxLength on the TextInput (the design-system field must pass it through to TextInput) and validate length before submit.",
     "Type the long value from the Steps into the field"),
    (r"does not reach", "app code",
     "The submit control does not lead where the code says it should. Check validation that fails silently, a navigation call that is not reached, and errors swallowed in the handler.",
     "Fill the form as in the Steps and tap '{name}'"),
    (r"^QA record (missing|still shown)", "app code",
     "Data the user created is not shown where it should be (or is still shown after it was deleted). Check that the record is written before navigating, that the list reloads on focus (useFocusEffect), and that relaunching reads the stored data.",
     "Run the journey's Steps, then open the screen named in Actual"),
    (r"^App fails during journey", "app code",
     "The app crashes or logs an error part-way through a user journey (details under Actual and in the device log). Catch the error in that step's handler and show the user what went wrong.",
     "Follow the journey's Steps and watch `adb logcat -s ReactNativeJS`"),
    (r"navigates to a screen that is not registered", "app code",
     "The handler navigates to a screen name the navigator does not declare, so the tap fails at runtime. Fix the name or add the <Stack.Screen>.",
     "Tap '{name}'"),
]


def _ref(file: str, line: int | None) -> str:
    return f"{file}:{line}" if file and line else file or ""


def _locate(repo: Path | None, rel: str, pattern: str) -> int | None:
    """1-based line of the first match of pattern in repo/rel (multi-line patterns allowed)."""
    if repo is None or not rel:
        return None
    path = repo / rel
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(pattern, text, re.S)
    if not m:
        return None
    return text.count("\n", 0, m.end()) + 1


def explain(findings: list[FindingRecord], *, inv: CodeInventory, components: list[ComponentRecord], screens: list[Screen], build: BuildInfo,
            signed_in: bool, repo: Path | None) -> None:
    comps = {c.key: c for c in components}
    by_label: dict[tuple[str, str], tuple[str, int | None]] = {}
    any_label: dict[str, tuple[str, int | None]] = {}
    for el in inv.elements:
        if el.label:
            by_label.setdefault((el.route, normalize(el.label)), (el.file, el.line))
            any_label.setdefault(normalize(el.label), (el.file, el.line))
    path_to = {}
    for s in screens:
        if s.route and s.route not in path_to:
            path_to[s.route] = [e.name for e in s.path if e.name]
    pkg, activity = build.app_id or "<package>", build.activity or ".MainActivity"
    device = {"android": "the Android device / emulator", "ios": "the iOS device / simulator", "macos": "this Mac"}.get(build.platform, "the device")

    for f in findings:
        name_m = QUOTED.search(f.title)
        name = name_m.group(1) if name_m else ""
        rule = next((r for r in RULES if re.search(r[0], f.title)), None)
        fill = lambda text: text.replace("{name}", name).replace("{route}", f.route).replace("{pkg}", pkg).replace("{activity}", activity)  # noqa: E731
        if rule and not f.fix:
            f.fix_in = f.fix_in or rule[1]
            f.fix = fill(rule[2])
        f.fix_in = f.fix_in or "app code"

        # where in the code
        if ":" not in f.code_ref:
            ref = f.code_ref  # a build finding names its own file
            comp = comps.get(f.component_key or "")
            password = next((e for e in inv.elements if e.kind == "input" and re.search("password", e.label or "", re.I)), None)
            if not ref and comp is not None and comp.code_file:
                ref = _ref(comp.code_file, comp.code_line)
            if not ref and name and (hit := by_label.get((f.route, normalize(name))) or any_label.get(normalize(name))):
                ref = _ref(*hit)
            if not ref and password is not None and re.search(r"[Pp]assword", f.title):
                ref = _ref(password.file, password.line)
            if not ref and f.route in inv.page_files:
                ref = inv.page_files[f.route]
            if ref and ":" not in ref and repo is not None:
                anchor = {"signed with the Android debug key": r"release\s*\{[^}]*?signingConfig\s+signingConfigs\.debug",
                          "can be copied out through backups": r"<application", "unencrypted HTTP": r"<application",
                          "state lost after rotation": r"MainActivity"}
                pat = next((p for k, p in anchor.items() if k in f.title), None)
                line = _locate(repo, ref, pat) if pat else None
                ref = _ref(ref, line)
            if repo is not None and ref and not (repo / ref.split(":")[0]).is_file():
                ref = ""  # never point at a file the repo does not have
            f.code_ref = ref

        # how to see it happen
        if not f.steps and f.viewport == "backend":
            f.steps = [fill(rule[3]) if rule else "Call the endpoint named in the title", "Compare with Expected; the numbers are under Actual"]
        if not f.steps:
            steps = [f"Install / open {build.label or pkg} ({pkg}) on {device}"]
            if build.platform == "android" and f.viewport != "build":
                steps.append("Keep the app's data (no clean start)")
            if signed_in and f.route not in ("", "app build") and not re.search(r"[Ss]ign-in|Password", f.title):
                steps.append("Sign in with the test account")
            for tap in path_to.get(f.route, []):
                steps.append(f"Tap '{tap}'")
            if f.route and not f.route.startswith("app build"):
                steps.append(f"You are on {f.route}")
            if rule:
                steps.append(fill(rule[3]))
            steps.append("Compare with Expected; the screenshot shows what the test saw")
            f.steps = steps
