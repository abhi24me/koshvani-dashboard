"""
Helper: prints every grant code + name available on the Koshvani portal, so you
can find the `grant_text` value to use in schemes.json.

Usage: python scraper/list_grants.py
"""
import sys
from playwright.sync_api import sync_playwright

MAIN_URL = "https://koshvani.up.nic.in/KoshvaniStatic.aspx"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MAIN_URL, wait_until="networkidle")
        for link in page.query_selector_all("body a"):
            if link.inner_text().strip() == "Grant-wise expenditure":
                link.click()
                break
        page.wait_for_load_state("networkidle")

        rows = page.query_selector_all("table tr")
        for row in rows:
            cells = row.query_selector_all("td")
            if not cells:
                continue
            link = row.query_selector("a")
            if link:
                code = link.inner_text().strip()
                texts = [c.inner_text().strip() for c in cells]
                print(f"{code}\t{' | '.join(t for t in texts if t and t != code)}")
        browser.close()


if __name__ == "__main__":
    main()
