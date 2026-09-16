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
"""
import json
import os
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

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


def scrape_scheme(scheme, attempts=3):
    """Retries with a fresh session (fresh cookie jar) each time - the portal
    occasionally bounces a request back to the main page (session hiccup /
    light throttling), and a brand new session usually clears it."""
    last_result = None
    for attempt in range(1, attempts + 1):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount("https://", _LEGACY_TLS_ADAPTER)
        try:
            last_result = _scrape_scheme_once(session, scheme)
        finally:
            session.close()
        if last_result["status"] != "error":
            return last_result
        print(f"    attempt {attempt}/{attempts} failed: {last_result['message']}", file=sys.stderr)
        if attempt < attempts:
            time.sleep(3)
    return last_result


def _scrape_scheme_once(session, scheme):
    try:
        r = session.get(MAIN_URL, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        fin_year = get_selected_fin_year(soup)

        href = find_link(soup, "body a", lambda t: t == "Grant-wise expenditure")
        if not href:
            raise RuntimeError("Could not find 'Grant-wise expenditure' link on main page")

        r = session.get(urljoin(r.url, href), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        if "ddlAmountIn" not in r.text:
            raise RuntimeError(f"Unexpected page after following 'Grant-wise expenditure': {r.url}")
        soup = BeautifulSoup(r.text, "html.parser")

        href = find_link(soup, "table a", lambda t: t == scheme["grant_text"])
        if not href:
            raise RuntimeError(f"Could not find grant link '{scheme['grant_text']}' on ExpGrant.aspx")

        r = session.get(urljoin(r.url, href), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        if "ExpHead" not in r.url:
            raise RuntimeError(f"Expected ExpHead.aspx after selecting grant, got {r.url}")
        soup = BeautifulSoup(r.text, "html.parser")

        href = find_link(soup, "table a", lambda t: t.startswith(scheme["scheme_code"]))
        if not href:
            raise RuntimeError(f"Could not find scheme code '{scheme['scheme_code']}' on ExpHead.aspx")

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
            raise RuntimeError(f"Expected ExpTreas.aspx after selecting scheme, got {r.url}")

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

    except (requests.exceptions.RequestException, RuntimeError) as exc:
        return {
            **base_meta(scheme, None),
            "status": "error",
            "message": str(exc),
            "column_headers": [],
            "column_widths": None,
            "rows": [],
            "totals": {},
        }


def base_meta(scheme, fin_year):
    return {
        "id": scheme["id"],
        "name": scheme["name"],
        "grant_text": scheme["grant_text"],
        "scheme_code": scheme["scheme_code"],
        "district": scheme["district"],
        "fin_year": fin_year,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    schemes = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    index = []
    for scheme in schemes:
        print(f"Scraping {scheme['id']} ({scheme['name']})...", file=sys.stderr)
        result = scrape_scheme(scheme)

        out_path = DATA_DIR / f"{scheme['id']}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        totals = result.get("totals", {})
        index.append({
            "id": result["id"],
            "name": result["name"],
            "grant_text": result["grant_text"],
            "scheme_code": result["scheme_code"],
            "district": result["district"],
            "status": result["status"],
            "generated_at": result["generated_at"],
            "fin_year": result.get("fin_year"),
            "progressive_allotment": totals.get("progressive_allotment"),
            "total_expenditure": totals.get("total_expenditure_upto_month"),
            "pct_expenditure_of_allotment": totals.get("pct_expenditure_of_allotment"),
        })
        print(f"  -> status={result['status']}", file=sys.stderr)

    (DATA_DIR / "index.json").write_text(
        json.dumps({"schemes": index, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
