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
  * one bad scheme never stops the others; a fatal error stops the loop but
    keeps everything already saved and leaves the rest at 0 (status PARTIAL).
"""
import json
import os
import random
import re
import signal
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "schemes.json"
DATA_DIR = ROOT / "docs" / "data"

BASE_URL = "https://koshvani.up.nic.in"
MAIN_URL = f"{BASE_URL}/KoshvaniStatic.aspx"
REQUEST_TIMEOUT = 45

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

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


_LEGACY_TLS_ADAPTER = LegacyTLSAdapter()

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
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= MAX_REMARK_LENGTH else text[: MAX_REMARK_LENGTH - 1] + "…"


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
    return result


def attempt_scheme(scheme):
    """One clean attempt: a brand-new session (fresh cookie jar) that follows
    the portal's click path from the start, so freshly generated links are
    used. The portal occasionally bounces a request back to the main page
    (session hiccup / light throttling) and a new session usually clears it."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", _LEGACY_TLS_ADAPTER)
    try:
        return validate_result(_scrape_scheme_once(session, scheme), scheme)
    finally:
        session.close()


def _scrape_scheme_once(session, scheme):
    r = session.get(MAIN_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    fin_year = get_selected_fin_year(soup)

    href = find_link(soup, "body a", lambda t: t == "Grant-wise expenditure")
    if not href:
        raise MissingLinkError("Could not find 'Grant-wise expenditure' link on main page")

    r = session.get(urljoin(r.url, href), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    if "ddlAmountIn" not in r.text:
        raise UnexpectedPageError(f"Unexpected page after following 'Grant-wise expenditure': {r.url}")
    soup = BeautifulSoup(r.text, "html.parser")

    href = find_link(soup, "table a", lambda t: t == scheme["grant_text"])
    if not href:
        raise MissingLinkError(f"Could not find grant link '{scheme['grant_text']}' on ExpGrant.aspx")

    r = session.get(urljoin(r.url, href), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    if "ExpHead" not in r.url:
        raise UnexpectedPageError(f"Expected ExpHead.aspx after selecting grant, got {r.url}")
    soup = BeautifulSoup(r.text, "html.parser")

    href = find_link(soup, "table a", lambda t: t.startswith(scheme["scheme_code"]))
    if not href:
        raise MissingLinkError(f"Could not find scheme code '{scheme['scheme_code']}' on ExpHead.aspx")

    r = session.get(urljoin(r.url, href), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    if "NoRecordFound" in r.url:
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
# mid-write can never leave a half-written JSON file behind.
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    json.loads(tmp.read_text(encoding="utf-8"))  # only ever publish a file that parses
    os.replace(tmp, path)


def save_result(scheme, result):
    write_json_atomic(DATA_DIR / f"{scheme['id']}.json", result)


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
        except OSError:
            pass


class CrawlStatus:
    """docs/data/crawler_status.json - the tracker for the CURRENT execution
    only (history lives in git). One entry per scheme, keyed by its numeric
    scheme code:  status 0 = not processed, 1 = success, -1 = failed."""

    def __init__(self, schemes):
        self.codes = [str(s["scheme_code"]) for s in schemes]
        self.path = DATA_DIR / "crawler_status.json"
        self.data = None

    def start(self):
        now = utc_now()
        self.data = {
            "execution_id": now,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "overall_status": "RUNNING",
            "fatal_error": "",
            "schemes": {c: {"status": STATUS_PENDING, "attempts": 0, "remark": ""} for c in self.codes},
        }
        self._save()

    def record(self, code, status, attempts, remark):
        self.data["schemes"][str(code)] = {"status": status, "attempts": attempts, "remark": remark}
        self._save()

    def status_of(self, scheme):
        return self.data["schemes"][str(scheme["scheme_code"])]["status"]

    def counts(self):
        entries = list(self.data["schemes"].values())
        return {
            "total": len(entries),
            "successful": sum(1 for e in entries if e["status"] == STATUS_OK),
            "failed": sum(1 for e in entries if e["status"] == STATUS_FAILED),
            "unprocessed": sum(1 for e in entries if e["status"] == STATUS_PENDING),
            "recovered": sum(1 for e in entries if e["status"] == STATUS_OK and e["attempts"] > 1),
        }

    def finalize(self, fatal_error=""):
        self.data["fatal_error"] = fatal_error
        self.data["overall_status"] = compute_overall(self.counts())
        now = utc_now()
        self.data["finished_at"] = now
        self._save(now)             # the last update IS the finish

    def _save(self, now=None):
        self.data["updated_at"] = now or utc_now()
        write_json_atomic(self.path, self.data)


def compute_overall(counts):
    if counts["total"] == 0:
        return "ERROR"          # nothing was even tracked (e.g. schemes.json unusable)
    if counts["unprocessed"] > 0:
        # Nothing attempted at all = the crawler itself failed; otherwise the run was cut short.
        return "ERROR" if counts["successful"] + counts["failed"] == 0 else "PARTIAL"
    return "WARNING" if counts["failed"] > 0 else "SUCCESS"


def write_index(schemes, crawl):
    """docs/data/index.json for the dashboard: the fields it has always had,
    plus the execution summary. Rebuilt from the scheme files after every
    scheme, so it always describes what is really on disk."""
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
            "crawl_status": crawl.status_of(scheme),
        })
    counts = crawl.counts()
    write_json_atomic(DATA_DIR / "index.json", {
        "schemes": entries,
        "generated_at": utc_now(),
        "execution_id": crawl.data["execution_id"],
        "overall_status": crawl.data["overall_status"],
        "expected_scheme_count": counts["total"],
        "successful_scheme_count": counts["successful"],
        "failed_scheme_count": counts["failed"],
        "unprocessed_scheme_count": counts["unprocessed"],
    })


# ---------------------------------------------------------------------------
# The crawl itself
# ---------------------------------------------------------------------------

def _log(message=""):
    print(message, file=sys.stderr, flush=True)


def _sleep(seconds):
    time.sleep(seconds)


def retry_delay(retry_number):
    """Randomized wait before a retry: 1 = before attempt 2, 2 = before attempt 3."""
    low, high = {1: (RETRY_1_DELAY_MIN, RETRY_1_DELAY_MAX)}.get(retry_number, (RETRY_2_DELAY_MIN, RETRY_2_DELAY_MAX))
    return round(random.uniform(low, high), 1)


def process_scheme(position, total, scheme, crawl):
    """One scheme, start to finish, within this execution: up to MAX_ATTEMPTS
    tries with a randomized pause between them. A scraping failure of ANY kind
    is contained here (it is this scheme's failure, nobody else's); a failure to
    persist is not - it propagates as a fatal error. Returns True on success."""
    code = str(scheme["scheme_code"])
    _log(f"[SCHEME {position}/{total}] {scheme['name']}")
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = retry_delay(attempt - 1)
            _log(f"  Waiting {delay:g} seconds before retry...")
            _sleep(delay)
        _log(f"  Attempt {attempt}/{MAX_ATTEMPTS}")
        try:
            result = attempt_scheme(scheme)
        except Exception as exc:
            last_error = describe_error(exc)
            _log(f"  Attempt {attempt}/{MAX_ATTEMPTS} failed: {last_error}")
            crawl.record(code, STATUS_FAILED, attempt, last_error)
            continue
        save_result(scheme, result)   # immediately - before anything else can go wrong
        if attempt > 1:
            remark = "Recovered on retry"
        else:
            remark = {"empty": "No expenditure recorded (portal reports no record)",
                      "no_district_data": "No district rows for this scheme"}.get(result["status"], "")
        crawl.record(code, STATUS_OK, attempt, remark)
        _log(f"  Attempt {attempt}/{MAX_ATTEMPTS} succeeded.")
        return True
    save_result(scheme, error_result(scheme, last_error))
    _log("  FINAL STATUS: FAILED")
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


def print_summary(counts, fatal_error, overall):
    _log()
    _log("=" * 50)
    _log("KOSHVANI CRAWLER SUMMARY")
    _log("=" * 50)
    _log(f"Total schemes: {counts['total']}")
    _log(f"Successful: {counts['successful']}")
    _log(f"Failed: {counts['failed']}")
    _log(f"Unprocessed: {counts['unprocessed']}")
    _log(f"Recovered by retry: {counts['recovered']}")
    _log(f"Fatal crawler error: {fatal_error or 'No'}")
    _log(f"Execution status: {overall}")
    _log("=" * 50)


def main(install_signals=True):
    """Returns the process exit code: 0 when every scheme was attempted
    (SUCCESS or WARNING), EXIT_INCOMPLETE when the run was cut short."""
    if install_signals:
        _install_signal_handlers()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean_stale_tmp_files()

    try:
        schemes = load_schemes()
    except Exception as exc:
        fatal = f"Fatal crawler error: {describe_error(exc)}"
        _log(fatal)
        try:   # record it as this execution's status, with no schemes to track
            crawl = CrawlStatus([])
            crawl.start()
            crawl.finalize(fatal)
        except Exception as write_exc:
            _log(f"Could not write crawler_status.json: {describe_error(write_exc)}")
        return EXIT_INCOMPLETE

    crawl = CrawlStatus(schemes)
    fatal = ""
    try:
        crawl.start()
        write_index(schemes, crawl)
        for position, scheme in enumerate(schemes, 1):
            process_scheme(position, len(schemes), scheme, crawl)
            write_index(schemes, crawl)
            _log()
    except (KeyboardInterrupt, SystemExit) as exc:
        fatal = f"Interrupted: {exc or type(exc).__name__}"
    except Exception as exc:
        fatal = f"Fatal crawler error: {describe_error(exc)}"
    if fatal:
        _log(f"\n{fatal}")

    try:
        crawl.finalize(fatal)
        write_index(schemes, crawl)
    except Exception as exc:
        _log(f"Could not finalize crawler_status.json / index.json: {describe_error(exc)}")
        return EXIT_INCOMPLETE

    print_summary(crawl.counts(), fatal, crawl.data["overall_status"])
    return EXIT_OK if crawl.data["overall_status"] in ("SUCCESS", "WARNING") else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
