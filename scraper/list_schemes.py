"""
Helper: given a grant code, prints every scheme code + name under it, so you
can find the `scheme_code` value to use in schemes.json.

Usage: python scraper/list_schemes.py 011
"""
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MAIN_URL = "https://koshvani.up.nic.in/KoshvaniStatic.aspx"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def main():
    if len(sys.argv) != 2:
        print("Usage: python scraper/list_schemes.py <grant_code>", file=sys.stderr)
        sys.exit(1)
    grant_code = sys.argv[1]

    session = requests.Session()
    session.headers.update(HEADERS)

    r = session.get(MAIN_URL, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    href = next(
        (a.get("href") for a in soup.select("body a") if a.get_text(strip=True) == "Grant-wise expenditure"),
        None,
    )
    if not href:
        print("Could not find 'Grant-wise expenditure' link", file=sys.stderr)
        sys.exit(1)

    r = session.get(urljoin(r.url, href), timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    href2 = next(
        (a.get("href") for a in soup.select("table a") if a.get_text(strip=True) == grant_code),
        None,
    )
    if not href2:
        print(f"Grant code '{grant_code}' not found on ExpGrant.aspx", file=sys.stderr)
        sys.exit(1)

    r = session.get(urljoin(r.url, href2), timeout=45)
    if "ExpHead" not in r.url:
        print(f"Unexpected page after selecting grant: {r.url}", file=sys.stderr)
        sys.exit(1)
    soup = BeautifulSoup(r.text, "html.parser")

    for link in soup.select("table a"):
        text = link.get_text(strip=True)
        if "=" in text:
            print(text)


if __name__ == "__main__":
    main()
