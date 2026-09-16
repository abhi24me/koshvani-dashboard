"""
Scrapes district-level (default: G.B.NAGAR) expenditure data from the
Koshvani (UP government finance) portal for a configured list of schemes,
and writes the results as JSON for the static dashboard in docs/.

The site is ASP.NET WebForms and requires a real browser session (cookies +
postbacks) - a plain HTTP GET returns a blank page. So for every scheme we
replay the actual click path in a headless browser:

    KoshvaniStatic.aspx
      -> click "Grant-wise expenditure"      -> ExpGrant.aspx
      -> click the grant code (e.g. "011")   -> ExpHead.aspx (scheme-code list)
      -> click the scheme code               -> ExpTreas.aspx (treasury/district
                                                 breakdown) or NoRecordFound.htm

The encrypted query-string tokens on these links are generated per-render by
the server, so they are never hardcoded here - we always click through fresh.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "schemes.json"
DATA_DIR = ROOT / "docs" / "data"

MAIN_URL = "https://koshvani.up.nic.in/KoshvaniStatic.aspx"
NAV_TIMEOUT_MS = 90_000

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
PCT_CELL_INDEX = 8
MIN_CELLS = 9


def to_number(text):
    text = (text or "").strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def extract_table_headers(page):
    """Pulls the report's own column header text verbatim from the DOM."""
    return page.eval_on_selector_all(
        "#Table1 tr:first-child th", "els => els.map(e => e.innerText.trim())"
    )


def extract_column_widths(page):
    """Pulls the report's own column width percentages (it sets width:X% per
    <th> itself), so the dashboard table keeps the site's proportions."""
    styles = page.eval_on_selector_all(
        "#Table1 tr:first-child th", "els => els.map(e => e.style.width || '')"
    )
    widths = []
    for s in styles:
        m = re.match(r"(\d+(?:\.\d+)?)%", s.strip())
        widths.append(float(m.group(1)) if m else None)
    if any(w is None for w in widths) or not widths:
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


def extract_district_rows(page, district):
    """Returns the district's rows exactly as the site renders the block:
    the district name appears only on the first row, blank on the rest -
    same as on koshvani.up.nic.in itself."""
    rows = page.eval_on_selector_all(
        "#myTable tr",
        "els => els.map(r => Array.from(r.querySelectorAll('td')).map(td => td.innerText.trim()))",
    )
    result = []
    current_district = None
    for cells in rows:
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


def get_selected_fin_year(page):
    try:
        return page.eval_on_selector(
            "#ddlFinYear", "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : null"
        )
    except Exception:
        return None


def click_exact_text_link(page, selector_scope, text):
    links = page.query_selector_all(f"{selector_scope} a")
    for link in links:
        if link.inner_text().strip() == text:
            link.click()
            return True
    return False


def click_prefix_text_link(page, selector_scope, prefix):
    links = page.query_selector_all(f"{selector_scope} a")
    for link in links:
        if link.inner_text().strip().startswith(prefix):
            link.click()
            return True
    return False


def scrape_scheme(browser, scheme, attempts=3):
    """Retries with a fresh browser context each time - the portal
    occasionally bounces a session back to the main page (session hiccup /
    light throttling), and a brand new session usually clears it."""
    last_result = None
    for attempt in range(1, attempts + 1):
        context = browser.new_context()
        try:
            last_result = _scrape_scheme_once(context, scheme)
        finally:
            context.close()
        if last_result["status"] != "error":
            return last_result
        print(f"    attempt {attempt}/{attempts} failed: {last_result['message']}", file=sys.stderr)
        if attempt < attempts:
            time.sleep(3)
    return last_result


def _scrape_scheme_once(context, scheme):
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page.set_default_timeout(NAV_TIMEOUT_MS)

    try:
        page.goto(MAIN_URL, wait_until="networkidle")
        fin_year = get_selected_fin_year(page)

        if not click_exact_text_link(page, "body", "Grant-wise expenditure"):
            raise RuntimeError("Could not find 'Grant-wise expenditure' link on main page")
        page.wait_for_load_state("networkidle")

        if not click_exact_text_link(page, "table", scheme["grant_text"]):
            raise RuntimeError(f"Could not find grant link '{scheme['grant_text']}' on ExpGrant.aspx")
        page.wait_for_load_state("networkidle")

        if "ExpHead" not in page.url:
            raise RuntimeError(f"Expected ExpHead.aspx after selecting grant, got {page.url}")

        if not click_prefix_text_link(page, "table", scheme["scheme_code"]):
            raise RuntimeError(f"Could not find scheme code '{scheme['scheme_code']}' on ExpHead.aspx")
        page.wait_for_load_state("networkidle")

        if "NoRecordFound" in page.url:
            return {
                **base_meta(scheme, fin_year),
                "status": "empty",
                "message": "No expenditure recorded for this scheme in the current period.",
                "column_headers": [],
                "column_widths": None,
                "rows": [],
                "totals": {},
            }

        if "ExpTreas" not in page.url:
            raise RuntimeError(f"Expected ExpTreas.aspx after selecting scheme, got {page.url}")

        headers = extract_table_headers(page)
        column_widths = extract_column_widths(page)
        month_labels = extract_month_labels(headers)
        rows = extract_district_rows(page, scheme["district"])
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

    except (PWTimeoutError, RuntimeError) as exc:
        return {
            **base_meta(scheme, None),
            "status": "error",
            "message": str(exc),
            "column_headers": [],
            "column_widths": None,
            "rows": [],
            "totals": {},
        }
    finally:
        page.close()


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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for scheme in schemes:
            print(f"Scraping {scheme['id']} ({scheme['name']})...", file=sys.stderr)
            result = scrape_scheme(browser, scheme)

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
        browser.close()

    (DATA_DIR / "index.json").write_text(
        json.dumps({"schemes": index, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
