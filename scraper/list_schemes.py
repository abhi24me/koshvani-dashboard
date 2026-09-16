"""
Helper: given a grant code, prints every scheme code + name under it, so you can
find the `scheme_code` value to use in schemes.json.

Usage: python scraper/list_schemes.py 011
"""
import sys
from playwright.sync_api import sync_playwright

MAIN_URL = "https://koshvani.up.nic.in/KoshvaniStatic.aspx"


def main():
    if len(sys.argv) != 2:
        print("Usage: python scraper/list_schemes.py <grant_code>", file=sys.stderr)
        sys.exit(1)
    grant_code = sys.argv[1]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MAIN_URL, wait_until="networkidle")
        for link in page.query_selector_all("body a"):
            if link.inner_text().strip() == "Grant-wise expenditure":
                link.click()
                break
        page.wait_for_load_state("networkidle")

        found = False
        for link in page.query_selector_all("table a"):
            if link.inner_text().strip() == grant_code:
                link.click()
                found = True
                break
        if not found:
            print(f"Grant code '{grant_code}' not found on ExpGrant.aspx", file=sys.stderr)
            sys.exit(1)
        page.wait_for_load_state("networkidle")

        if "ExpHead" not in page.url:
            print(f"Unexpected page after selecting grant: {page.url}", file=sys.stderr)
            sys.exit(1)

        for link in page.query_selector_all("table a"):
            text = link.inner_text().strip()
            if "=" in text:
                print(text)
        browser.close()


if __name__ == "__main__":
    main()
