#!/usr/bin/env python3
"""Log sanitizer: nothing sensitive may ever reach a log file, the screen or a commit.

Removes cookies, authorization headers, tokens, API keys, passwords, session ids,
the portal's encrypted query parameters (URL queries are dropped altogether), e-mail
addresses, well-known credential formats, and every value found in .env.

Used three ways (standard library only, so it runs unchanged on Termux):

    import logsafe; logsafe.scrub(text)          # the scraper's / mailer's log formatter
    ... | python -u logsafe.py | tee -a log      # run_daily.sh: filters a stream, line by line
    python logsafe.py --scrub-file PATH          # rewrite a file in place (last line of defence)
    python logsafe.py --check PATH               # exit 1 if scrubbing would still change PATH
"""
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
REDACTED = "<redacted>"
MIN_SECRET_LENGTH = 6            # shorter .env values (e.g. "true", "4") would mangle ordinary log text

_KEYS = (r"password|passwd|pwd|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
         r"auth|cookie|session[_-]?id|sessionid|sid|jsessionid|asp\.net_sessionid|__viewstate\w*|__eventvalidation|"
         r"__requestverificationtoken|enc|encrypted\w*|signature|sig|credential\w*|client[_-]?secret")

_COOKIE_LINE = re.compile(r"(?i)\b(set-)?cookie\s*[:=].*")
_AUTH_HEADER = re.compile(r"(?i)\b(proxy-)?authorization\s*[:=]\s*(?:(?:bearer|basic|digest|token)\s+)?\S+")
_KEY_VALUE = re.compile(rf"(?i)\b(?P<key>{_KEYS})\b(?P<sep>\s*[=:]\s*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s&;,\"'<>]+)")
_URL = re.compile(r"https?://[^\s'\"<>)\]]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_KNOWN_TOKENS = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|\b\d{6,}:[A-Za-z0-9_-]{30,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}|\bxox[baprs]-[A-Za-z0-9-]{10,}|\bsk-[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_/.\\-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9_/.\\-])")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_env_values = None
_env_override = None


def configure(env_path=None):
    """Point the sanitizer at a different .env (tests) and drop the cache."""
    global _env_values, _env_override
    _env_override = Path(env_path) if env_path else None
    _env_values = None


def _load_env_values():
    """Every value of .env (plus obviously secret-looking process variables): all of it is secret."""
    values = set()
    path = _env_override or (ROOT / ".env")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.partition("=")[2].strip().strip('"').strip("'")
        values.add(value)
        values.update(part.strip() for part in value.split(","))
        values.add("".join(value.split()))               # an app password shown in spaced groups
    for name, value in os.environ.items():
        if re.search(r"(?i)password|token|secret|api[_-]?key|recipient|sender|chat_id", name):
            values.add(value.strip())
    return sorted((v for v in values if len(v) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def _clean_url(match):
    text = match.group(0)
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        keep_marker = f"?{REDACTED}" if parts.query else ("?" if text.endswith("?") else "")   # a 2nd pass sees the bare '?'
        return f"{parts.scheme}://{host}{port}{parts.path}{keep_marker}"
    except ValueError:
        return f"{text.split('?', 1)[0]}" + (f"?{REDACTED}" if "?" in text else "")


def _long_token(match):
    token = match.group(0)
    if _GIT_SHA.match(token) or not (re.search(r"\d", token) and re.search(r"[A-Za-z]", token)):
        return token
    return REDACTED


def scrub(text):
    """Return `text` with everything sensitive replaced. Idempotent."""
    global _env_values
    if not text:
        return text
    text = str(text)
    if _env_values is None:
        _env_values = _load_env_values()
    for value in _env_values:
        text = text.replace(value, REDACTED)
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _URL.sub(_clean_url, text)                    # userinfo and every query string go
    text = _COOKIE_LINE.sub(lambda m: f"{m.group(0).split(':')[0].split('=')[0].strip()}: {REDACTED}", text)
    text = _AUTH_HEADER.sub(lambda m: f"Authorization: {REDACTED}", text)
    text = _KEY_VALUE.sub(lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text)
    text = _KNOWN_TOKENS.sub(REDACTED, text)
    text = _LONG_TOKEN.sub(_long_token, text)
    text = _EMAIL.sub("<email>", text)
    return text


class SanitizingFormatter(logging.Formatter):
    """A log formatter that sanitizes the finished line - message, arguments and traceback alike."""

    def format(self, record):
        return scrub(super().format(record))


def _filter_stream():
    src, dst = sys.stdin.buffer, sys.stdout.buffer
    for raw in src:
        try:
            line = scrub(raw.decode("utf-8", errors="replace"))
        except Exception:                                # a broken sanitizer must never leak, nor kill the run
            line = "[log line withheld: sanitizer error]\n"
        try:
            dst.write(line.encode("utf-8", errors="replace"))
            dst.flush()
        except BrokenPipeError:
            return


def _scrub_file(path):
    path = Path(path)
    original = path.read_text(encoding="utf-8", errors="replace")
    cleaned = scrub(original)
    if cleaned != original:
        tmp = path.with_name(path.name + ".scrub.tmp")
        tmp.write_text(cleaned, encoding="utf-8", newline="")
        os.replace(tmp, path)
    return cleaned != original


def main(argv):
    if len(argv) >= 3 and argv[1] == "--scrub-file":
        changed = _scrub_file(argv[2])
        print(f"[logsafe] {argv[2]}: {'sanitized in place' if changed else 'already clean'}", file=sys.stderr)
        return 0
    if len(argv) >= 3 and argv[1] == "--check":
        text = Path(argv[2]).read_text(encoding="utf-8", errors="replace")
        return 0 if scrub(text) == text else 1
    _filter_stream()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
