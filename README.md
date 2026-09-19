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
On Windows, double-click `refresh.bat` to do all four steps in one go (only
pushes if the data actually changed).

### Automatic updates from a phone (Termux)

A phone on mobile data is a genuine residential-class connection too, and
running a plain Python script is much lighter than trying to run a full
browser or a GitHub Actions runner on Android. `run_daily.sh` (repo root)
does the same pull-scrape-commit-push cycle as `refresh.bat`, meant to run
on a schedule via [Termux](https://termux.dev/) + Termux:Boot. If a run is cut
short (killed by Android, a fatal error), what it had already scraped is
committed and pushed anyway, and leftovers from a killed run are saved as a
commit rather than discarded:

```
pkg install python git -y
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r scraper/requirements.txt
bash run_daily.sh
```

One TLS quirk specific to this: koshvani.up.nic.in's server doesn't
correctly support secure TLS renegotiation, which newer OpenSSL builds
(like Termux's) reject by default - `scraper/scrape.py` already works around
this with a small `LegacyTLSAdapter` that enables
`SSL_OP_LEGACY_SERVER_CONNECT` (certificate verification stays fully on;
this doesn't disable any security checks, it only permits a legacy
renegotiation behavior the site's server needs). Desktop Python/OpenSSL
builds don't need this flag, so it's a safe no-op there.

An earlier attempt used a genuine GitHub Actions self-hosted runner instead
(so the dashboard's "Run workflow" button would trigger it), but hit
persistent Windows-specific issues (runner service account permissions,
antivirus file locks) and was abandoned in favor of the simpler script
above.

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

Writes `docs/data/index.json`, one `docs/data/<id>.json` per scheme, and
`docs/data/crawler_status.json` (see below). You can open `docs/index.html`
via a local static server (e.g. `python -m http.server` from inside `docs/`)
to preview before pushing.

## How a crawl is tracked

Every scheme is an independent unit, and the run is built so that nothing
already scraped can be lost:

- **Per-scheme retries, in the same run.** Each scheme gets up to 3 attempts
  (`MAX_ATTEMPTS`), one scheme at a time - there is no separate retry pass
  afterwards, and a scheme that succeeded is never retried. Every attempt uses
  a fresh session (fresh cookies and freshly generated portal links). The wait
  before attempt 2 is random 5-15 s and before attempt 3 random 15-30 s
  (`RETRY_1_DELAY_*`, `RETRY_2_DELAY_*` at the top of `scraper/scrape.py`).
- **Saved immediately.** A scheme's `docs/data/<id>.json` is written (atomically:
  temp file, then `os.replace`) the moment it has been scraped and validated,
  so a later failure or crash can never take it away. `index.json` is refreshed
  after every scheme, so it never claims more than what is really on disk.
- **`docs/data/crawler_status.json`** - exactly one file, describing the
  *current* execution only (git history keeps the previous ones), reset at the
  start of every run. Keyed by the numeric scheme code from `schemes.json`:

  ```json
  {
    "execution_id": "2026-09-19T14:30:12+00:00",
    "started_at": "2026-09-19T14:30:12+00:00",
    "updated_at": "2026-09-19T14:32:41+00:00",
    "finished_at": "2026-09-19T14:32:41+00:00",
    "overall_status": "WARNING",
    "fatal_error": "",
    "schemes": {
      "2401000010500": {"status": 1,  "attempts": 1, "remark": ""},
      "2401001020103": {"status": 1,  "attempts": 2, "remark": "Recovered on retry"},
      "2401001020129": {"status": -1, "attempts": 3, "remark": "ReadTimeout: HTTPSConnectionPool(...): Read timed out."}
    }
  }
  ```

  `status`: **1** = scraped and validated, **-1** = failed (after the last
  attempt it stays -1 with `attempts: 3` and the latest error), **0** = never
  processed (only possible if the run was cut short, which makes the overall
  status `PARTIAL`). It never stores links,
  cookies or sessions; URLs in error messages are reduced to the page name.
- **Overall status:** `SUCCESS` (all 24 = 1), `WARNING` (all attempted, some
  -1), `PARTIAL` (cut short by a fatal error or a kill - some still 0),
  `ERROR` (nothing could be attempted). The scraper exits 0 for the first two
  and 3 otherwise; either way everything it saved is kept.
- **Reports.** Telegram and Gmail are generated from this *final* state, after
  the retries: a scheme that failed once and then recovered is not a failure.
  Telegram stays short; the Gmail message carries the full 24-scheme table
  (status, attempts, remark for every scheme).
- **Baseline.** `.koshvani_previous_data/` still advances per scheme, only for
  schemes freshly scraped in that run with healthy data, and only after the
  report was delivered (updating it inside the scraper would erase the
  before/after that "Data changes" needs).

## Running the tests

```
python -m unittest discover -s tests -v
```

Standard library only and fully offline (fake portal, SMTP and Telegram; every
test works in a temp directory). The `run_daily.sh` tests need `bash` and `git`
and the dashboard tests need Playwright; each skips itself when unavailable.

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
