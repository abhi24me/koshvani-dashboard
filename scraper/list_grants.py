"""
Helper: prints every grant code + name available on the Koshvani portal, so you
can find the `grant_text` value to use in schemes.json.

Usage: python scraper/list_grants.py
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

    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        link = row.find("a")
        if link:
            code = link.get_text(strip=True)
            texts = [c.get_text(" ", strip=True) for c in cells]
            print(f"{code}\t{' | '.join(t for t in texts if t and t != code)}")


if __name__ == "__main__":
    main()
