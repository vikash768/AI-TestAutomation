#!/usr/bin/env bash
# Safe pre-push repository hygiene checks. Reports locations/categories only.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

status=0
tracked_findings="$(mktemp "${TMPDIR:-/tmp}/ai-testautomation-tracked.XXXXXX")"
source_findings="$(mktemp "${TMPDIR:-/tmp}/ai-testautomation-source.XXXXXX")"
trap 'rm -f "$tracked_findings" "$source_findings"' EXIT

report_tracked_file() {
    printf '%s — %s\n' "$1" "$2" >> "$tracked_findings"
}

while IFS= read -r path; do
    case "$path" in
        .env|*/.env|.env.*|*/.env.*)
            case "$path" in
                .env.example|*/.env.example) ;;
                *) report_tracked_file "$path" "tracked environment file" ;;
            esac
            ;;
        artifacts/cache/*|artifacts/runs/*|artifacts/screenshots/*|artifacts/logs/*|artifacts/robot/*|artifacts/repos/*|artifacts/builds/*)
            report_tracked_file "$path" "tracked generated artifact"
            ;;
        *.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*/id_rsa|*/id_ed25519)
            report_tracked_file "$path" "tracked private key or signing material"
            ;;
        *service-account*.json|*service_account*.json|*google-credentials*.json|*gcp-credentials*.json|*credentials.local.json|*credentials.*.local.json)
            report_tracked_file "$path" "tracked credential JSON"
            ;;
        */.pytest_cache/*|*/.mypy_cache/*|*/.ruff_cache/*|*/__pycache__/*|*.pyc|*.pyo|*/.idea/*|*/.vscode/*|*/.DS_Store)
            report_tracked_file "$path" "tracked local development output"
            ;;
        *.apk|*.aab|*.ipa|*.xcarchive/*|*.app/*|*.msi)
            report_tracked_file "$path" "tracked mobile or desktop build artifact"
            ;;
    esac
done < <(git ls-files)

if [ -s "$tracked_findings" ]; then
    echo "ERROR: dangerous tracked paths detected:"
    sort -u "$tracked_findings" | sed 's/^/  /'
    status=1
fi

check_ignored() {
    probe="$1"
    category="$2"
    if ! git check-ignore -q --no-index "$probe"; then
        echo "ERROR: $probe — missing $category ignore rule"
        status=1
    fi
}

check_ignored ".env" "environment-file"
check_ignored ".env.local" "environment-file"
check_ignored "artifacts/cache/security-check-probe.json" "generated artifact"
check_ignored "artifacts/runs/security-check-probe.json" "generated artifact"
check_ignored "artifacts/screenshots/security-check-probe.png" "generated artifact"
check_ignored "artifacts/logs/security-check-probe.log" "generated artifact"
check_ignored "artifacts/robot/output.xml" "Robot artifact"
check_ignored "security-check-probe.pem" "private-key"
check_ignored "service-account-security-check.json" "credential-JSON"

scan_pattern() {
    category="$1"
    pattern="$2"
    git grep -n -I -E -e "$pattern" -- . \
        ':(exclude).env' \
        ':(exclude).env.*' \
        ':(exclude)artifacts/**' \
        ':(exclude)scripts/security-check.sh' 2>/dev/null \
        | while IFS=: read -r path line _; do
            printf '%s:%s — possible %s\n' "$path" "$line" "$category"
        done >> "$source_findings"
}

# These patterns identify recognizable credential formats without printing matches.
scan_pattern "private key header" '-----BEGIN ([A-Z0-9]+ )?PRIVATE KEY-----'
scan_pattern "AWS access key literal" 'AKIA[0-9A-Z]{16}'
scan_pattern "Google API key literal" 'AIza[0-9A-Za-z_-]{30,}'
scan_pattern "GitHub token literal" 'gh[pousr]_[0-9A-Za-z]{20,}'
scan_pattern "Slack token literal" 'xox[baprs]-[0-9A-Za-z-]{10,}'
scan_pattern "Bearer token literal" '[Bb]earer[[:space:]]+[0-9A-Za-z._~+/-]{20,}'

if [ -s "$source_findings" ]; then
    echo "ERROR: possible credential literals detected in tracked source:"
    sort -u "$source_findings" | sed 's/^/  /'
    status=1
fi

if ! git diff --check; then
    echo "ERROR: git diff --check failed"
    status=1
fi

if [ "$status" -eq 0 ]; then
    echo "Security check passed: tracked paths, ignore rules, source patterns, and diff whitespace are safe."
fi

exit "$status"
