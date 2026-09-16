import requests
from bs4 import BeautifulSoup
import time

BASE = "https://koshvani.up.nic.in"
MAIN_URL = f"{BASE}/KoshvaniStatic.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def find_link_by_exact_text(soup, text):
    for a in soup.find_all("a"):
        if a.get_text(strip=True) == text:
            return a.get("href")
    return None


def find_link_by_prefix(soup, prefix):
    for a in soup.find_all("a"):
        if a.get_text(strip=True).startswith(prefix):
            return a.get("href")
    return None


def resolve(base_url, href):
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def main():
    s = requests.Session()
    s.headers.update(HEADERS)

    t0 = time.time()

    # Step 1: main page
    r = s.get(MAIN_URL, timeout=30)
    print(f"[1] main page: {r.status_code}, {len(r.content)} bytes, {time.time()-t0:.1f}s")
    soup = BeautifulSoup(r.text, "html.parser")
    href = find_link_by_exact_text(soup, "Grant-wise expenditure")
    print(f"    found link: {href}")
    if not href:
        print("FAILED: could not find Grant-wise expenditure link")
        return

    # Step 2: ExpGrant.aspx
    url2 = resolve(r.url, href)
    r2 = s.get(url2, timeout=30)
    print(f"[2] ExpGrant: {r2.status_code}, final url: {r2.url}, {time.time()-t0:.1f}s")
    soup2 = BeautifulSoup(r2.text, "html.parser")
    href2 = find_link_by_exact_text(soup2, "011")
    print(f"    found grant '011' link: {href2[:80] if href2 else None}...")
    if not href2:
        print("FAILED: could not find grant 011 link - bounced back?")
        print("    page title snippet:", soup2.title.get_text() if soup2.title else None)
        return

    # Step 3: follow to ExpHead.aspx
    url3 = resolve(r2.url, href2)
    r3 = s.get(url3, timeout=30)
    print(f"[3] after grant click: {r3.status_code}, final url: {r3.url}, {time.time()-t0:.1f}s")
    if "ExpHead" not in r3.url:
        print("FAILED: did not land on ExpHead.aspx")
        return
    soup3 = BeautifulSoup(r3.text, "html.parser")
    href3 = find_link_by_prefix(soup3, "2401000010500")
    print(f"    found scheme link: {href3[:80] if href3 else None}...")
    if not href3:
        print("FAILED: could not find scheme code link")
        return

    # Step 4: follow to ExpTreas.aspx
    url4 = resolve(r3.url, href3)
    r4 = s.get(url4, timeout=30)
    print(f"[4] after scheme click: {r4.status_code}, final url: {r4.url}, {time.time()-t0:.1f}s")

    if "G.B.NAGAR" in r4.text:
        idx = r4.text.find("G.B.NAGAR")
        print("SUCCESS: found G.B.NAGAR in response!")
        print(r4.text[idx-50:idx+200])
    else:
        print("G.B.NAGAR not found in final page. URL:", r4.url)
        print("Page snippet:", r4.text[:300])

    print(f"\nTotal time: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
