# Koshvani G.B.Nagar Dashboard

A personal shortcut dashboard for the UP government finance portal
([koshvani.up.nic.in](https://koshvani.up.nic.in)). Instead of clicking through
Grant -> Scheme -> Treasury reports every time, this pulls the **G.B.NAGAR**
rows for a configured list of schemes and shows them on one page, on any
device.

It's built to run at **zero ongoing cost**:

- A scraper (Python + `requests` + BeautifulSoup) replays the site's actual
  click path (it requires a session cookie, but is otherwise plain
  server-rendered HTML - no browser or JavaScript execution needed) and
  writes the results as JSON.
- A static dashboard (plain HTML/CSS/JS, no backend) reads that JSON and is
  hosted for free on [GitHub Pages](https://pages.github.com/) — reachable
  from any device, anytime, independent of the scraper.

### Keeping the dashboard updated

koshvani.up.nic.in's firewall drops the connection outright (TCP timeout,
before any HTTP request) from every cloud/datacenter network tested: GitHub's
own hosted runners, Google Cloud, 12 other independent countries (including
an India-based one), and multiple residential-proxy services - confirmed at
the raw network layer (plain `requests`, no browser, still blocked instantly
from a cloud runner) so no client-side trick fixes it. Only genuine
residential/office connections get through - so **the reliable way to
refresh data today is running the scraper on a normal home/office connection
and pushing the result**:

```
python scraper/scrape.py
git add docs/data
git commit -m "Update data"
git push
```

That's it - the live Pages site updates within a minute or two of the push.

A GitHub Actions workflow (`.github/workflows/scrape.yml`) that does this
automatically on a self-hosted runner is included and partly set up, but
currently has unresolved Windows-specific issues on this machine (runner
service account permissions, antivirus file locks) and isn't relied on yet -
treat it as a future improvement, not the current path. The manual command
above is what actually keeps this dashboard current.

## One-time setup

1. **Create a GitHub repository** (public, so Pages is free) and push this
   project to it:
   ```
   git init
   git add .
   git commit -m "Initial Koshvani dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
2. **Enable GitHub Pages**: repo Settings -> Pages -> Source: "Deploy from a
   branch" -> Branch: `main`, folder `/docs` -> Save. Your dashboard will be
   live at `https://<your-username>.github.io/<repo-name>/`.
3. Run `python scraper/scrape.py` locally once (see "Running the scraper
   locally" below) and push - the Pages site now has real data.

From then on, refresh whenever you want by running the same command and
pushing again.

## Adding more schemes

Each entry in [scraper/schemes.json](scraper/schemes.json) is one card on the
dashboard:

```json
{
  "id": "agri-011-2401000010500",
  "name": "कृषि विभाग — जिला संगठन (Grant 011)",
  "grant_text": "011",
  "scheme_code": "2401000010500",
  "district": "G.B.NAGAR"
}
```

To find the `grant_text` and `scheme_code` for a new scheme, run the two
helper scripts locally (they just print lists, they don't write anything):

```
pip install -r scraper/requirements.txt

python scraper/list_grants.py            # lists every grant code + name
python scraper/list_schemes.py 011        # lists every scheme code + name under grant 011
```

Pick the grant code (e.g. `011`) and the numeric prefix of the scheme you
want (e.g. `2401000010500` from `2401000010500=जिला संगठन`), add a new object
to `schemes.json` with a unique `id`, then run `python scraper/scrape.py` and
push - see "Keeping the dashboard updated" above. You can also just paste the
scheme name/link to Claude in a future session and ask it to add it for you.

## Running the scraper locally

```
python scraper/scrape.py
```

Writes `docs/data/index.json` and one `docs/data/<id>.json` per scheme. You
can open `docs/index.html` via a local static server (e.g.
`python -m http.server` from inside `docs/`) to preview before pushing.

## Notes

- The detail table is copied **verbatim** from the site: column headers are
  scraped directly from the report's own `<th>` text (not retyped), and every
  cell is the exact scraped string, unmodified — same labels, same numbers,
  same formatting quirks (e.g. `.00` vs `0`), same district-name-only-on-the-
  first-row pattern the site itself uses. The three stat tiles above the
  table (Allotment / Expenditure / % Spent) are the one addition: numbers
  this dashboard computes by summing those exact rows, clearly labeled as
  such.
- The theme button in the header cycles system → light → dark and remembers
  your choice per browser.
- The site's report-page URLs contain encrypted, per-render query tokens —
  the scraper never hardcodes these; it always follows the real click path
  (main page -> Grant-wise expenditure -> grant -> scheme) in a fresh
  session each time.
- No headless browser is used - the scraper is a lightweight `requests` +
  BeautifulSoup script, since the site is fully server-rendered HTML (no
  JavaScript needed for content). This was verified by porting the whole
  flow from an earlier Playwright-based version and confirming identical
  output across every scheme.
- If a scheme shows "No expenditure" or "No district rows", that reflects
  the portal itself having no recorded spend for that scheme/district in the
  current period — not a bug.
