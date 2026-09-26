"""
Scrapes district-level (default: G.B.NAGAR) expenditure data from the
Koshvani (UP government finance) portal for a configured list of schemes,
and writes the results as JSON for the static dashboard in docs/.

The site is ASP.NET WebForms and requires a session cookie (a plain GET
without one bounces back to the main page) - but it's fully server-rendered
HTML, no client-side JS needed for content. So instead of a real browser,
this replays the click path with plain HTTP requests (requests.Session()
handles the cookie jar automatically, same as a browser would) and parses
the HTML with BeautifulSoup:

    KoshvaniStatic.aspx
      -> "Grant-wise expenditure" link  -> ExpGrant.aspx
      -> the grant code (e.g. "011")    -> ExpHead.aspx (scheme-code list)
      -> the scheme code                -> ExpTreas.aspx (treasury/district
                                            breakdown) or NoRecordFound.htm

The encrypted query-string tokens on these links are generated per-render by
the server, so they are never hardcoded here - we always follow them fresh
from whatever the previous page actually rendered.

Note: koshvani.up.nic.in's firewall blocks connections from cloud/datacenter
IP ranges outright (confirmed extensively - see README), so this only works
from a genuine residential/office network connection, not from GitHub's
hosted runners or any other cloud provider.

Reliability model - every scheme is an independent unit:

  * each scheme gets up to MAX_ATTEMPTS (3) tries, retried immediately inside
    its own turn (fresh session each time, randomized pause in between) - there
    is no separate retry phase after the loop, and a success is never retried;
  * a scheme's data file is written the moment it is scraped and validated
    (atomically), so a later failure or crash can never lose it;
  * docs/data/crawler_status.json (ONE file, current execution only) tracks each
    scheme by its numeric scheme code: 0 = not processed, 1 = success,
    -1 = failed, with the attempt count and the latest error;
  * one bad scheme never stops the others; a fatal error stops the run but
    keeps everything already saved and leaves the rest at 0 (status PARTIAL);
  * schemes run concurrently on a ThreadPoolExecutor (MAX_WORKERS threads),
    each attempt in its own session with its own TLS adapter; the status file is
    updated under a lock, and only the main thread ever writes index.json;
  * everything is logged in detail (every attempt, every HTTP request, timings,
    file writes, status updates, performance metrics), and every log line is
    sanitized by logsafe.py first.
"""
import json
import logging
import os
import random
import re
import signal
import ssl
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "schemes.json"
DATA_DIR = ROOT / "docs" / "data"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import logsafe  # noqa: E402  - the shared log sanitizer lives at the repo root

BASE_URL = "https://koshvani.up.nic.in"
MAIN_URL = f"{BASE_URL}/KoshvaniStatic.aspx"
REQUEST_TIMEOUT = 45

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# How many schemes are scraped at the same time. Every worker runs its own
# schemes with fresh sessions of its own; raise it cautiously - the portal is
# already flaky under sequential access.
MAX_WORKERS = 4

# Politeness towards a flaky portal. No two attempts (of any worker, retries included)
# start within STAGGER_SECONDS (+ up to STAGGER_JITTER random) of each other, so the
# workers never hit it in the same second. When the portal answers with its
# "ClearSession" bounce - which the logs show hits every session at once - ALL workers
# hold for BOUNCE_HOLD_MIN..MAX seconds instead of retrying on their own short timers.
STAGGER_SECONDS = 4
STAGGER_JITTER = 2
BOUNCE_HOLD_MIN = 60
BOUNCE_HOLD_MAX = 120

# After the first pass, schemes that still failed get COOLDOWN_ROUNDS more rounds
# (each a full MAX_ATTEMPTS-attempt turn) after a COOLDOWN_WAIT-second pause: the
# portal's bad spells last minutes, longer than the 5-30 s retry waits can outlast.
COOLDOWN_ROUNDS = 2
COOLDOWN_WAIT = 300

# Per-scheme retry policy. Retries happen immediately, inside the same
# execution and the same scheme's turn - never in a separate pass afterwards.
MAX_ATTEMPTS = 3                                # total tries per scheme (1 initial + 2 retries)
RETRY_1_DELAY_MIN, RETRY_1_DELAY_MAX = 5, 15    # seconds to wait before attempt 2 (randomized)
RETRY_2_DELAY_MIN, RETRY_2_DELAY_MAX = 15, 30   # seconds to wait before attempt 3 (randomized)
MAX_REMARK_LENGTH = 300                         # bound on a stored error message

STATUS_PENDING, STATUS_OK, STATUS_FAILED = 0, 1, -1
EXIT_OK, EXIT_INCOMPLETE = 0, 3


class LegacyTLSAdapter(HTTPAdapter):
    """Enables SSL_OP_LEGACY_SERVER_CONNECT, needed on newer OpenSSL builds
    (confirmed via Termux: Python 3.14 / OpenSSL 3.6.3) to complete the TLS
    handshake with this portal's server, which doesn't correctly support
    secure renegotiation. Certificate verification is untouched - still on."""

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


# Index of the numeric columns within a row's raw `cells` array, used only to
# compute the derived totals block - the cells themselves are stored verbatim,
# exactly as the site renders them (same text, same "0"/".00" inconsistencies
# and all), never reformatted or relabeled.
NUMERIC_CELL_INDEXES = {
    "progressive_allotment": 4,
    "expenditure_upto_prev_month": 5,
    "current_month_expenditure": 6,
    "total_expenditure_upto_month": 7,
}
MIN_CELLS = 9


def to_number(text):
    text = (text or "").strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def extract_table_headers(soup):
    """Pulls the report's own column header text verbatim from the DOM."""
    row = soup.select_one("#Table1 tr:first-child")
    if not row:
        return []
    return [th.get_text(strip=True) for th in row.find_all("th")]


def extract_column_widths(soup):
    """Pulls the report's own column width percentages (it sets width:X% per
    <th> itself), so the dashboard table keeps the site's proportions."""
    row = soup.select_one("#Table1 tr:first-child")
    if not row:
        return None
    widths = []
    for th in row.find_all("th"):
        style = th.get("style", "") or ""
        m = re.match(r"width:\s*(\d+(?:\.\d+)?)%", style.strip())
        widths.append(float(m.group(1)) if m else None)
    if not widths or any(w is None for w in widths):
        return None
    return widths


def extract_month_labels(header_cells):
    labels = {"prev_month": None, "current_month": None}
    if len(header_cells) >= 7:
        m = re.search(r"\(([A-Za-z]+)\)", header_cells[5])
        if m:
            labels["prev_month"] = m.group(1)
        m = re.search(r"\(([A-Za-z]+)\)", header_cells[6])
        if m:
            labels["current_month"] = m.group(1)
    return labels


def extract_district_rows(soup, district):
    """Returns the district's rows exactly as the site renders the block:
    the district name appears only on the first row, blank on the rest -
    same as on koshvani.up.nic.in itself."""
    result = []
    current_district = None
    for tr in soup.select("#myTable tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < MIN_CELLS:
            continue
        first_cell = cells[0].strip()
        if first_cell:
            current_district = first_cell
        if current_district != district:
            continue
        result.append({"cells": cells})
    return result


def compute_totals(rows):
    totals = {col: 0.0 for col in NUMERIC_CELL_INDEXES}
    for row in rows:
        cells = row["cells"]
        for col, idx in NUMERIC_CELL_INDEXES.items():
            totals[col] += to_number(cells[idx])
    allotment = totals["progressive_allotment"]
    spent = totals["total_expenditure_upto_month"]
    totals["pct_expenditure_of_allotment"] = round((spent / allotment) * 100, 2) if allotment else 0.0
    return totals


def get_selected_fin_year(soup):
    select = soup.select_one("#ddlFinYear")
    if not select:
        return None
    option = select.select_one("option[selected]") or select.select_one("option")
    return option.get_text(strip=True) if option else None


def find_link(soup, css_selector, predicate):
    """css_selector is a full CSS selector for the <a> tags to search, e.g.
    "table a" - a descendant selector matching links inside ANY table on the
    page, not just the first one."""
    for a in soup.select(css_selector):
        if predicate(a.get_text(strip=True)):
            return a.get("href")
    return None


class ScrapeError(RuntimeError):
    """A scheme-level failure whose message is already a useful diagnosis."""


class MissingLinkError(ScrapeError):
    """An expected link was not on the page the portal rendered."""


class UnexpectedPageError(ScrapeError):
    """The portal answered with a different page than the click path should reach."""


class ValidationError(ScrapeError):
    """The page loaded but the parsed result is not the expected structure."""


# ---------------------------------------------------------------------------
# Logging. Every line is sanitized by logsafe (no cookies, tokens, encrypted
# query parameters, passwords or .env values can be written), and carries the
# time, level, worker thread and - inside a scheme - "[position/total code]".
# ---------------------------------------------------------------------------

log = logging.getLogger("koshvani")
_ctx = threading.local()          # the scheme this worker thread is currently on
_STOP = threading.Event()         # set when the run must wind down (fatal error / interrupt)


def setup_logging():
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logsafe.SanitizingFormatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def say(message="", level=logging.INFO):
    log.log(level, "%s%s", getattr(_ctx, "tag", ""), message)


_URL_RE = re.compile(r"https?://[^\s'\")\]]+")
_URL_PATH_RE = re.compile(r"url: (/[^\s'\")\]]*)")


def describe_error(exc):
    """'ReadTimeout: ...' - the exception class plus its message, bounded in
    length. Every URL is reduced to its page name ('ExpHead.aspx'): the
    portal's links carry encrypted per-render tokens, which must never end up
    in crawler_status.json or the published scheme JSON."""
    text = f"{type(exc).__name__}: {exc}"
    text = _URL_RE.sub(lambda m: urlparse(m.group(0)).path.rsplit("/", 1)[-1] or urlparse(m.group(0)).netloc, text)
    text = _URL_PATH_RE.sub(lambda m: "url: " + (m.group(1).split("?")[0].rsplit("/", 1)[-1] or "/"), text)
    text = logsafe.scrub(re.sub(r"\s+", " ", text).strip())       # belt and braces: remarks are committed to git
    return text if len(text) <= MAX_REMARK_LENGTH else text[: MAX_REMARK_LENGTH - 1] + "…"


def classify_error(exc):
    """A short machine-friendly kind for logs and the performance summary."""
    if isinstance(exc, ScrapeError):
        return {"MissingLinkError": "missing-link", "UnexpectedPageError": "unexpected-page",
                "ValidationError": "validation"}.get(type(exc).__name__, "scrape-error")
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return f"http-{code}" if code else "http-error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        return "connection-reset" if ("reset" in text or "aborted" in text) else "connection-error"
    return type(exc).__name__.lower()


class Metrics:
    """Thread-safe counters for the final performance summary."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = self.request_failures = self.bytes = 0
        self.latencies, self.schemes, self.error_kinds = [], [], {}
        self.retry_wait = 0.0
        self.gate_wait, self.gate_waits, self.bounces = 0.0, 0, 0

    def gated(self, seconds):
        with self._lock:
            self.gate_wait += seconds
            self.gate_waits += 1

    def bounce(self):
        with self._lock:
            self.bounces += 1

    def request(self, seconds, nbytes=0, failed=False):
        with self._lock:
            self.requests += 1
            self.request_failures += 1 if failed else 0
            self.bytes += nbytes
            self.latencies.append(seconds)

    def failure(self, kind):
        with self._lock:
            self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1

    def waited(self, seconds):
        with self._lock:
            self.retry_wait += seconds

    def scheme(self, worker, code, seconds, attempts, ok):
        with self._lock:
            self.schemes.append({"worker": worker, "code": code, "seconds": seconds, "attempts": attempts, "ok": ok})

    def summary_lines(self, workers, wall):
        with self._lock:
            lines = [f"Wall time: {wall:.1f}s | workers: {workers} | max attempts per scheme: {MAX_ATTEMPTS}"]
            if self.schemes:
                secs = [s["seconds"] for s in self.schemes]
                lines.append(f"Scheme time: min {min(secs):.1f}s | avg {sum(secs) / len(secs):.1f}s | max {max(secs):.1f}s")
                slowest = sorted(self.schemes, key=lambda s: -s["seconds"])[:3]
                lines.append("Slowest schemes: " + ", ".join(
                    f"{s['code']} {s['seconds']:.1f}s (attempts {s['attempts']}, {s['worker']})" for s in slowest))
                per_worker = {}
                for s in self.schemes:
                    count, busy = per_worker.get(s["worker"], (0, 0.0))
                    per_worker[s["worker"]] = (count + 1, busy + s["seconds"])
                utilisation = sum(secs) / (workers * wall) * 100 if wall > 0 else 0
                lines.append(f"Worker utilisation: {utilisation:.0f}% | " + ", ".join(
                    f"{w}: {c} schemes {t:.0f}s" for w, (c, t) in sorted(per_worker.items())))
            lat = sorted(self.latencies)
            lines.append(f"HTTP requests: {self.requests} (ok {self.requests - self.request_failures}, "
                         f"failed {self.request_failures}) | downloaded {self.bytes / 1024:.0f} KB")
            if lat:
                lines.append(f"Request latency: avg {sum(lat) / len(lat):.2f}s | p95 {lat[min(len(lat) - 1, int(len(lat) * 0.95))]:.2f}s | max {lat[-1]:.2f}s")
            if self.error_kinds:
                lines.append("Attempt failures by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(self.error_kinds.items())))
            lines.append(f"Time spent waiting between retries: {self.retry_wait:.1f}s (summed over workers)")
            lines.append(f"Start stagger / bounce hold: {self.gate_waits} waits, {self.gate_wait:.1f}s (summed over workers) | "
                         f"portal bounces seen: {self.bounces}")
            return lines


_METRICS = Metrics()


class StartGate:
    """Spreads out the starts of attempts across ALL workers, and lets every worker
    hold together when the portal is having one of its bad spells."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._hold_until = 0.0

    def reserve(self):
        """Seconds this attempt must wait for its start slot (0 = go now)."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + STAGGER_SECONDS + (random.uniform(0, STAGGER_JITTER) if STAGGER_JITTER else 0)
            return slot - now if STAGGER_SECONDS else 0.0

    def hold(self, seconds):
        with self._lock:
            self._hold_until = max(self._hold_until, time.monotonic() + seconds)

    def hold_remaining(self):
        with self._lock:
            return max(0.0, self._hold_until - time.monotonic())


_GATE = StartGate()


def wait_for_turn():
    """Called before every attempt: the stagger slot first, then any portal-wide hold."""
    began = time.monotonic()
    delay = _GATE.reserve()
    if delay > 0:
        say(f"Start stagger: waiting {delay:.1f}s so workers do not hit the portal together")
        _sleep(delay)
    hold = _GATE.hold_remaining()
    if hold > 0:
        say(f"Portal hold: the portal is bouncing sessions, waiting {hold:.0f}s with the other workers", logging.WARNING)
        _sleep(hold)
    waited = time.monotonic() - began
    if delay > 0 or hold > 0:
        _METRICS.gated(waited)


def _note_portal_bounce(where):
    """The portal bounced this session to its ClearSession page. The logs show it does that to
    every session at once, so everybody pauses, not just this worker."""
    seconds = round(random.uniform(BOUNCE_HOLD_MIN, BOUNCE_HOLD_MAX), 0)
    _GATE.hold(seconds)
    _METRICS.bounce()
    say(f"PORTAL BOUNCE at {where}: all workers will hold for up to {seconds:g}s", logging.WARNING)


def _page_name(url):
    parts = urlsplit(str(url))
    return parts.path.rsplit("/", 1)[-1] or parts.netloc or "?"


def _fetch(session, url, what):
    """One logged HTTP GET: start, then end (status, size, elapsed) or the failure
    kind. The URL is only ever logged as its page name - never its query string."""
    page = _page_name(url)
    say(f"HTTP GET {what} ({page}) START")
    started = time.monotonic()
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        elapsed = time.monotonic() - started
        _METRICS.request(elapsed, failed=True)
        say(f"HTTP GET {what} ({page}) FAILED after {elapsed:.2f}s kind={classify_error(exc)}: {describe_error(exc)}", logging.WARNING)
        raise
    elapsed = time.monotonic() - started
    size = len(response.content)
    _METRICS.request(elapsed, size)
    redirects = len(response.history)
    extra = f" redirects={redirects} final={_page_name(response.url)}" if redirects else ""
    say(f"HTTP GET {what} ({page}) END status={response.status_code} bytes={size} elapsed={elapsed:.2f}s{extra}")
    if "ClearSession" in str(response.url) or (size < 2000 and "ClearSession" in response.text):
        _note_portal_bounce(what)
    return response


def _describe_page(soup, size):
    """What a page that lacks the expected report table actually is - to diagnose it later."""
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = " ".join(soup.get_text(" ", strip=True).split())[:160]
    return (f"title={title!r} bytes={size} has_Table1={bool(soup.select_one('#Table1'))} "
            f"has_myTable={bool(soup.select_one('#myTable'))} text={text!r}")


def validate_result(result, scheme):
    """A result only counts as a success if it is the structure the dashboard
    expects; anything else is an attempt failure (and gets retried)."""
    status = result.get("status")
    if status not in ("ok", "empty", "no_district_data"):
        raise ValidationError(f"Unexpected result status {status!r}")
    if result.get("id") != scheme["id"] or str(result.get("scheme_code")) != str(scheme["scheme_code"]):
        raise ValidationError("Result does not match the requested scheme")
    if status in ("ok", "no_district_data") and not result.get("column_headers"):
        raise ValidationError("Missing expected table: no column headers found on the report page")
    if status == "ok":
        rows = result.get("rows") or []
        if not rows:
            raise ValidationError("Report table has no district rows")
        if any(len(r.get("cells", [])) < MIN_CELLS for r in rows):
            raise ValidationError("Malformed report row (too few cells)")
        totals = result.get("totals") or {}
        if any(k not in totals for k in (*NUMERIC_CELL_INDEXES, "pct_expenditure_of_allotment")):
            raise ValidationError("Totals could not be computed from the report rows")
    say(f"VALIDATE ok status={status} rows={len(result.get('rows') or [])}")
    return result


def attempt_scheme(scheme):
    """One clean attempt: a brand-new session (fresh cookie jar) that follows
    the portal's click path from the start, so freshly generated links are
    used. The portal occasionally bounces a request back to the main page
    (session hiccup / light throttling) and a new session usually clears it.
    The session gets its OWN TLS adapter: closing a session closes its adapters,
    which would tear down a shared connection pool under the other workers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", LegacyTLSAdapter())
    try:
        return validate_result(_scrape_scheme_once(session, scheme), scheme)
    finally:
        session.close()


def _scrape_scheme_once(session, scheme):
    r = _fetch(session, MAIN_URL, "main page")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    fin_year = get_selected_fin_year(soup)

    href = find_link(soup, "body a", lambda t: t == "Grant-wise expenditure")
    if not href:
        raise MissingLinkError("Could not find 'Grant-wise expenditure' link on main page")

    r = _fetch(session, urljoin(r.url, href), "grant list")
    r.raise_for_status()
    if "ddlAmountIn" not in r.text:
        raise UnexpectedPageError(f"Unexpected page after following 'Grant-wise expenditure': {r.url}")
    soup = BeautifulSoup(r.text, "html.parser")

    href = find_link(soup, "table a", lambda t: t == scheme["grant_text"])
    if not href:
        raise MissingLinkError(f"Could not find grant link '{scheme['grant_text']}' on ExpGrant.aspx")

    r = _fetch(session, urljoin(r.url, href), "scheme list")
    r.raise_for_status()
    if "ExpHead" not in r.url:
        raise UnexpectedPageError(f"Expected ExpHead.aspx after selecting grant, got {r.url}")
    soup = BeautifulSoup(r.text, "html.parser")

    href = find_link(soup, "table a", lambda t: t.startswith(scheme["scheme_code"]))
    if not href:
        raise MissingLinkError(f"Could not find scheme code '{scheme['scheme_code']}' on ExpHead.aspx")

    r = _fetch(session, urljoin(r.url, href), "scheme report")
    r.raise_for_status()

    if "NoRecordFound" in r.url:
        say("PARSE portal answered NoRecordFound (no expenditure recorded for this scheme)")
        return {
            **base_meta(scheme, fin_year),
            "status": "empty",
            "message": "No expenditure recorded for this scheme in the current period.",
            "column_headers": [],
            "column_widths": None,
            "rows": [],
            "totals": {},
        }

    if "ExpTreas" not in r.url:
        raise UnexpectedPageError(f"Expected ExpTreas.aspx after selecting scheme, got {r.url}")

    soup = BeautifulSoup(r.text, "html.parser")
    headers = extract_table_headers(soup)
    column_widths = extract_column_widths(soup)
    month_labels = extract_month_labels(headers)
    rows = extract_district_rows(soup, scheme["district"])
    totals = compute_totals(rows) if rows else {}
    if headers:
        say(f"PARSE report table: columns={len(headers)} district_rows={len(rows)} fin_year={fin_year} months={month_labels}")
    else:
        say(f"PARSE report page has NO table header row: {_describe_page(soup, len(r.content))}", logging.WARNING)

    return {
        **base_meta(scheme, fin_year),
        "status": "ok" if rows else "no_district_data",
        "message": None if rows else f"No {scheme['district']} rows found for this scheme in the current period.",
        "prev_month": month_labels.get("prev_month"),
        "current_month": month_labels.get("current_month"),
        "column_headers": headers,
        "column_widths": column_widths,
        "rows": rows,
        "totals": totals,
    }


def base_meta(scheme, fin_year):
    return {
        "id": scheme["id"],
        "name": scheme["name"],
        "grant_text": scheme["grant_text"],
        "scheme_code": scheme["scheme_code"],
        "district": scheme["district"],
        "fin_year": fin_year,
        "generated_at": utc_now(),
    }


def error_result(scheme, message):
    """What a scheme's data file holds after it failed every attempt - the
    same shape as always, so the dashboard shows its 'Fetch error' state."""
    return {
        **base_meta(scheme, None),
        "status": "error",
        "message": message,
        "column_headers": [],
        "column_widths": None,
        "rows": [],
        "totals": {},
    }


# ---------------------------------------------------------------------------
# Persistence: every write is atomic (temp file, then os.replace), so a kill
# mid-write can never leave a half-written JSON file behind. Each writer uses
# its own temp name, so concurrent workers can never trample each other's.
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_execution_id():
    """Unique per run: local time + 6 random hex digits, e.g. 20260922-101530-a1b2c3."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def write_json_atomic(path, data):
    """Returns (bytes written, seconds taken)."""
    started = time.monotonic()
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    json.loads(tmp.read_text(encoding="utf-8"))  # only ever publish a file that parses
    for tries in range(6):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:      # Windows: the destination is briefly open elsewhere (a reader, an antivirus scan)
            if tries == 5:
                raise
            time.sleep(0.02 * (tries + 1))
    return len(payload.encode("utf-8")), time.monotonic() - started


def save_result(scheme, result):
    size, seconds = write_json_atomic(DATA_DIR / f"{scheme['id']}.json", result)
    say(f"FILE WRITE {scheme['id']}.json {size} bytes {seconds * 1000:.0f} ms "
        f"(status={result.get('status')}, rows={len(result.get('rows') or [])})")


GOOD_STATUSES = ("ok", "empty", "no_district_data")


def keep_last_good(scheme, error):
    """A scheme that failed every attempt must not destroy what the dashboard already
    shows: if its data file holds real data from an earlier run, that file is kept
    untouched (crawler_status.json still records the failure, so reports and the
    baseline are not fooled). Only a scheme with nothing good on disk gets the
    error stub that makes the dashboard show its 'Fetch error' state."""
    previous = load_result(scheme)
    if previous and previous.get("status") in GOOD_STATUSES:
        say(f"KEEPING last good data for {scheme['id']} (from {previous.get('generated_at')}); "
            f"the failure is only recorded in crawler_status.json", logging.WARNING)
        return
    save_result(scheme, error_result(scheme, error))


def load_result(scheme):
    try:
        return json.loads((DATA_DIR / f"{scheme['id']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clean_stale_tmp_files():
    """A run killed mid-write can leave a *.tmp behind; it is never valid data."""
    for tmp in DATA_DIR.glob("*.tmp"):
        try:
            tmp.unlink()
            say(f"CLEANUP removed stale temp file {tmp.name}", logging.WARNING)
        except OSError:
            pass


def _counts(entries):
    entries = list(entries)
    return {
        "total": len(entries),
        "successful": sum(1 for e in entries if e["status"] == STATUS_OK),
        "failed": sum(1 for e in entries if e["status"] == STATUS_FAILED),
        "unprocessed": sum(1 for e in entries if e["status"] == STATUS_PENDING),
        "recovered": sum(1 for e in entries if e["status"] == STATUS_OK and e["attempts"] > 1),
    }


class CrawlStatus:
    """docs/data/crawler_status.json - the tracker for the CURRENT execution
    only (history lives in git). One entry per scheme, keyed by its numeric
    scheme code:  status 0 = not processed, 1 = success, -1 = failed.

    Shared by all worker threads: every read and write happens under one lock,
    and the file itself is replaced atomically, so it is always complete and valid."""

    def __init__(self, schemes):
        self.codes = [str(s["scheme_code"]) for s in schemes]
        self.path = DATA_DIR / "crawler_status.json"
        self.data = None
        self._lock = threading.RLock()

    def start(self, execution_id=None):
        with self._lock:
            now = utc_now()
            self.data = {
                "execution_id": execution_id or new_execution_id(),
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "overall_status": "RUNNING",
                "fatal_error": "",
                "schemes": {c: {"status": STATUS_PENDING, "attempts": 0, "remark": ""} for c in self.codes},
            }
            size, seconds = self._save()
            say(f"STATUS RESET execution_id={self.data['execution_id']} schemes={len(self.codes)} all set to status=0 "
                f"[crawler_status.json {size} bytes, {seconds * 1000:.0f} ms]")

    def record(self, code, status, attempts, remark):
        with self._lock:
            self.data["schemes"][str(code)] = {"status": status, "attempts": attempts, "remark": remark}
            size, seconds = self._save()
            say(f"STATUS UPDATE scheme={code} status={status} attempts={attempts} remark={remark!r} "
                f"[crawler_status.json {size} bytes, {seconds * 1000:.0f} ms]")

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.data))

    def status_of(self, scheme):
        with self._lock:
            return self.data["schemes"][str(scheme["scheme_code"])]["status"]

    def counts(self):
        with self._lock:
            return _counts(self.data["schemes"].values())

    def finalize(self, fatal_error=""):
        with self._lock:
            self.data["fatal_error"] = fatal_error
            self.data["overall_status"] = compute_overall(_counts(self.data["schemes"].values()))
            now = utc_now()
            self.data["finished_at"] = now
            size, seconds = self._save(now)             # the last update IS the finish
            say(f"STATUS FINAL overall={self.data['overall_status']} fatal={fatal_error or 'no'} "
                f"[crawler_status.json {size} bytes, {seconds * 1000:.0f} ms]")

    def _save(self, now=None):
        """Callers hold the lock. Returns (bytes, seconds)."""
        self.data["updated_at"] = now or utc_now()
        return write_json_atomic(self.path, self.data)


def compute_overall(counts):
    if counts["total"] == 0:
        return "ERROR"          # nothing was even tracked (e.g. schemes.json unusable)
    if counts["unprocessed"] > 0:
        # Nothing attempted at all = the crawler itself failed; otherwise the run was cut short.
        return "ERROR" if counts["successful"] + counts["failed"] == 0 else "PARTIAL"
    return "WARNING" if counts["failed"] > 0 else "SUCCESS"


def write_index(schemes, crawl):
    """docs/data/index.json for the dashboard: the fields it has always had,
    plus the execution summary. Only ever written by the coordinating (main)
    thread, from a consistent snapshot of the status and the scheme files on
    disk - the workers never touch it."""
    snap = crawl.snapshot()
    entries = []
    for scheme in schemes:
        result = load_result(scheme) or {**base_meta(scheme, None), "status": "pending", "generated_at": None, "totals": {}}
        totals = result.get("totals") or {}
        entries.append({
            "id": scheme["id"],
            "name": scheme["name"],
            "grant_text": scheme["grant_text"],
            "scheme_code": scheme["scheme_code"],
            "district": scheme["district"],
            "status": result.get("status"),
            "generated_at": result.get("generated_at"),
            "fin_year": result.get("fin_year"),
            "progressive_allotment": totals.get("progressive_allotment"),
            "total_expenditure": totals.get("total_expenditure_upto_month"),
            "pct_expenditure_of_allotment": totals.get("pct_expenditure_of_allotment"),
            "crawl_status": snap["schemes"][str(scheme["scheme_code"])]["status"],
        })
    counts = _counts(snap["schemes"].values())
    size, seconds = write_json_atomic(DATA_DIR / "index.json", {
        "schemes": entries,
        "generated_at": utc_now(),
        "execution_id": snap["execution_id"],
        "overall_status": snap["overall_status"],
        "expected_scheme_count": counts["total"],
        "successful_scheme_count": counts["successful"],
        "failed_scheme_count": counts["failed"],
        "unprocessed_scheme_count": counts["unprocessed"],
    })
    say(f"FILE WRITE index.json {size} bytes {seconds * 1000:.0f} ms | overall={snap['overall_status']} "
        f"successful={counts['successful']} failed={counts['failed']} unprocessed={counts['unprocessed']}")


# ---------------------------------------------------------------------------
# The crawl itself
# ---------------------------------------------------------------------------

def _sleep(seconds):
    _STOP.wait(seconds)         # a plain sleep, except that a stop request wakes it early


def retry_delay(retry_number):
    """Randomized wait before a retry: 1 = before attempt 2, 2 = before attempt 3."""
    low, high = {1: (RETRY_1_DELAY_MIN, RETRY_1_DELAY_MAX)}.get(retry_number, (RETRY_2_DELAY_MIN, RETRY_2_DELAY_MAX))
    return round(random.uniform(low, high), 1)


def process_scheme(position, total, scheme, crawl, round_no=1):
    """One scheme, start to finish, on whichever worker thread picked it up: up
    to MAX_ATTEMPTS tries with a randomized pause between them. A scraping
    failure of ANY kind is contained here (it is this scheme's failure, nobody
    else's). Anything that escapes - a failure to persist, an interrupt - is
    fatal for the whole run: the stop flag is raised right here, from the
    worker itself, so no other worker or queued scheme carries on meanwhile.
    Returns True on success."""
    try:
        return _process_scheme(position, total, scheme, crawl, round_no)
    except BaseException:
        _STOP.set()
        raise


def _process_scheme(position, total, scheme, crawl, round_no=1):
    code = str(scheme["scheme_code"])
    worker = threading.current_thread().name
    _ctx.tag = f"[{position:02d}/{total:02d} {code}] "
    if _STOP.is_set():
        say("SCHEME SKIPPED: the run is winding down before this scheme started", logging.WARNING)
        return False
    began = time.monotonic()
    prior = (round_no - 1) * MAX_ATTEMPTS          # attempts spent in earlier rounds: the status shows the running total
    round_note = f" [cool-down round {round_no - 1}/{COOLDOWN_ROUNDS}]" if round_no > 1 else ""
    say(f"SCHEME START{round_note} {scheme['name']} (worker={worker})")
    last_error, attempts_made = "", 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            if _STOP.is_set():
                say("SCHEME STOPPED: the run is winding down, no further attempts", logging.WARNING)
                return False
            delay = retry_delay(attempt - 1)
            say(f"Waiting {delay:g} seconds before retry...")
            _METRICS.waited(delay)
            _sleep(delay)
        attempts_made = attempt
        wait_for_turn()
        say(f"Attempt {attempt}/{MAX_ATTEMPTS} START")
        attempt_began = time.monotonic()
        try:
            result = attempt_scheme(scheme)
        except Exception as exc:
            last_error = describe_error(exc)
            kind = classify_error(exc)
            _METRICS.failure(kind)
            say(f"Attempt {attempt}/{MAX_ATTEMPTS} failed after {time.monotonic() - attempt_began:.2f}s "
                f"kind={kind}: {last_error}", logging.WARNING)
            crawl.record(code, STATUS_FAILED, prior + attempt, last_error)
            continue
        save_result(scheme, result)   # immediately - before anything else can go wrong
        if round_no > 1:
            remark = f"Recovered in cool-down round {round_no - 1}"
        elif attempt > 1:
            remark = "Recovered on retry"
        else:
            remark = {"empty": "No expenditure recorded (portal reports no record)",
                      "no_district_data": "No district rows for this scheme"}.get(result["status"], "")
        crawl.record(code, STATUS_OK, prior + attempt, remark)
        elapsed = time.monotonic() - began
        say(f"Attempt {attempt}/{MAX_ATTEMPTS} succeeded in {time.monotonic() - attempt_began:.2f}s")
        say(f"SCHEME DONE status=1 attempts={prior + attempt} elapsed={elapsed:.2f}s worker={worker}")
        _METRICS.scheme(worker, code, elapsed, prior + attempt, True)
        return True
    keep_last_good(scheme, last_error)
    elapsed = time.monotonic() - began
    say(f"FINAL STATUS: FAILED after {prior + attempts_made} attempts")
    say(f"SCHEME DONE status=-1 attempts={prior + attempts_made} elapsed={elapsed:.2f}s worker={worker}", logging.WARNING)
    _METRICS.scheme(worker, code, elapsed, prior + attempts_made, False)
    return False


def load_schemes():
    schemes = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(schemes, list) or not schemes:
        raise ValueError("scraper/schemes.json is empty or not a list of schemes")
    codes = [str(s["scheme_code"]) for s in schemes]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise ValueError(f"Duplicate scheme_code in schemes.json: {', '.join(duplicates)}")
    return schemes


def run_workers(schemes, crawl, workers):
    """Runs every scheme on a pool of `workers` threads and returns the fatal
    error text ('' if none). Workers only ever touch their own scheme's file
    and the locked status; index.json is rebuilt here, on the main thread, as
    each scheme completes and once more when all have finished.

    After the first pass, schemes that still failed get up to COOLDOWN_ROUNDS more
    rounds, each after a COOLDOWN_WAIT pause (a full MAX_ATTEMPTS-attempt turn each)."""
    fatal, total = "", len(schemes)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="worker")

    def run_pass(items, round_no):
        nonlocal fatal
        futures = [pool.submit(process_scheme, i, total, s, crawl, round_no) for i, s in items]
        for future in as_completed(futures):
            if future.cancelled():
                continue
            try:
                future.result()
            except Exception as exc:        # scraping failures never get here; a failure to persist does
                if not fatal:
                    fatal = f"Fatal crawler error: {describe_error(exc)}"
                    say(fatal, logging.ERROR)
                    _STOP.set()
                    for pending in futures:
                        pending.cancel()
            write_index(schemes, crawl)

    try:
        run_pass(list(enumerate(schemes, 1)), 1)
        for round_no in range(2, COOLDOWN_ROUNDS + 2):
            if fatal or _STOP.is_set():
                break
            failed = [(i, s) for i, s in enumerate(schemes, 1) if crawl.status_of(s) == STATUS_FAILED]
            if not failed:
                break
            say(f"COOL-DOWN round {round_no - 1}/{COOLDOWN_ROUNDS}: {len(failed)} scheme(s) still failed; "
                f"waiting {COOLDOWN_WAIT}s for the portal to settle", logging.WARNING)
            _sleep(COOLDOWN_WAIT)
            if _STOP.is_set():
                break
            run_pass(failed, round_no)
    except (KeyboardInterrupt, SystemExit) as exc:
        _STOP.set()
        fatal = f"Interrupted: {exc or type(exc).__name__}"
        say(fatal, logging.ERROR)
    finally:
        # In-flight schemes finish their current attempt (so nothing they scraped is lost
        # and the status stays consistent); queued ones are dropped and stay at status 0.
        pool.shutdown(wait=True, cancel_futures=True)
    return fatal


def _install_signal_handlers():
    """Termux closing the session (SIGHUP) or a plain kill (SIGTERM) still
    gets to finalize the status file instead of dying silently."""
    def _stop(signum, frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass


def print_summary(counts, fatal_error, overall, execution_id, workers, wall):
    say("=" * 50)
    say("KOSHVANI CRAWLER SUMMARY")
    say("=" * 50)
    say(f"Execution ID: {execution_id}")
    say(f"Total schemes: {counts['total']}")
    say(f"Successful: {counts['successful']}")
    say(f"Failed: {counts['failed']}")
    say(f"Unprocessed: {counts['unprocessed']}")
    say(f"Recovered by retry: {counts['recovered']}")
    say(f"Fatal crawler error: {fatal_error or 'No'}")
    say(f"Execution status: {overall}")
    for line in _METRICS.summary_lines(workers, wall):
        say(line)
    say("=" * 50)


def main(install_signals=True):
    """Returns the process exit code: 0 when every scheme was attempted
    (SUCCESS or WARNING), EXIT_INCOMPLETE when the run was cut short."""
    global _METRICS, _GATE
    setup_logging()
    _STOP.clear()
    _METRICS = Metrics()
    _GATE = StartGate()
    _ctx.tag = ""
    started = time.monotonic()
    if install_signals:
        _install_signal_handlers()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    execution_id = os.environ.get("KOSHVANI_EXECUTION_ID") or new_execution_id()
    say("=" * 50)
    say("KOSHVANI CRAWLER START")
    say(f"Execution ID: {execution_id}")
    say(f"Python {sys.version.split()[0]} | requests {requests.__version__} | time zone offset {time.strftime('%z')}")
    clean_stale_tmp_files()

    try:
        schemes = load_schemes()
    except Exception as exc:
        fatal = f"Fatal crawler error: {describe_error(exc)}"
        say(fatal, logging.ERROR)
        try:   # record it as this execution's status, with no schemes to track
            crawl = CrawlStatus([])
            crawl.start(execution_id)
            crawl.finalize(fatal)
        except Exception as write_exc:
            say(f"Could not write crawler_status.json: {describe_error(write_exc)}", logging.ERROR)
        return EXIT_INCOMPLETE

    crawl = CrawlStatus(schemes)
    workers = max(1, min(MAX_WORKERS, len(schemes)))
    say(f"Config: schemes={len(schemes)} workers={workers} max_attempts={MAX_ATTEMPTS} "
        f"retry_delays={RETRY_1_DELAY_MIN}-{RETRY_1_DELAY_MAX}s/{RETRY_2_DELAY_MIN}-{RETRY_2_DELAY_MAX}s "
        f"request_timeout={REQUEST_TIMEOUT}s stagger={STAGGER_SECONDS}+0-{STAGGER_JITTER}s "
        f"bounce_hold={BOUNCE_HOLD_MIN}-{BOUNCE_HOLD_MAX}s cooldown_rounds={COOLDOWN_ROUNDS}x{COOLDOWN_WAIT}s")
    fatal = ""
    try:
        crawl.start(execution_id)
        write_index(schemes, crawl)
        fatal = run_workers(schemes, crawl, workers)
    except (KeyboardInterrupt, SystemExit) as exc:
        fatal = f"Interrupted: {exc or type(exc).__name__}"
    except Exception as exc:
        fatal = f"Fatal crawler error: {describe_error(exc)}"
    if fatal:
        say(fatal, logging.ERROR)

    try:
        crawl.finalize(fatal)
        write_index(schemes, crawl)
    except Exception as exc:
        say(f"Could not finalize crawler_status.json / index.json: {describe_error(exc)}", logging.ERROR)
        return EXIT_INCOMPLETE

    print_summary(crawl.counts(), fatal, crawl.data["overall_status"], execution_id, workers, time.monotonic() - started)
    return EXIT_OK if crawl.data["overall_status"] in ("SUCCESS", "WARNING") else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
