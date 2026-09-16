# Koshvani G.B.Nagar Dashboard

A personal shortcut dashboard for the UP government finance portal
([koshvani.up.nic.in](https://koshvani.up.nic.in)). Instead of clicking through
Grant -> Scheme -> Treasury reports every time, this pulls the **G.B.NAGAR**
rows for a configured list of schemes and shows them on one page, on any
device.

It's built to run at **zero ongoing cost**:

- A scraper (Python + Playwright) drives a real headless browser through the
  site's actual click path (the site requires session cookies, so a plain
  HTTP fetch of the report pages returns nothing) and writes the results as
  JSON.
- A [GitHub Actions](https://github.com/features/actions) workflow runs that
  scraper twice a day on a free schedule, and can also be triggered manually
  any time you want fresher data.
- A static dashboard (plain HTML/CSS/JS, no backend) reads that JSON and is
  hosted for free on [GitHub Pages](https://pages.github.com/).

## One-time setup

1. **Create a GitHub repository** (public, so Actions minutes and Pages are
   free) and push this project to it:
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
3. **Run the workflow once manually** to generate the first data files:
   repo -> Actions tab -> "Update Koshvani Data" -> Run workflow. Wait for it
   to finish (a couple of minutes), then refresh your Pages URL.

That's it — from then on it refreshes itself automatically twice a day
(~10:00 and ~18:00 IST), and you can always trigger an extra refresh from
the Actions tab (works from the GitHub mobile app too) if you want it sooner.

## Enabling the in-page "Refresh" button (optional)

The dashboard has a "Refresh" button with a loading spinner that re-scrapes
live and updates the page automatically once done. It needs a place to hold
a GitHub token that can start the Actions workflow — a browser page can't
hold that secret itself, so this uses a small [Cloudflare
Workers](https://workers.cloudflare.com/) proxy (free tier: 100,000
requests/day, no credit card required).

1. Create a free Cloudflare account, then install Wrangler:
   ```
   npm install -g wrangler
   wrangler login
   ```
2. In `cloudflare-worker/wrangler.toml`, fill in `GH_OWNER`, `GH_REPO`, and
   `ALLOWED_ORIGIN` (your `https://<user>.github.io` Pages origin).
3. Create a GitHub **fine-grained personal access token**
   (github.com -> Settings -> Developer settings -> Fine-grained tokens):
   scope it to this one repository only, with **Actions: Read and write**
   permission, nothing else.
4. Deploy the worker and set the token as a secret:
   ```
   cd cloudflare-worker
   wrangler deploy
   wrangler secret put GH_TOKEN
   ```
5. Wrangler prints your worker's URL, e.g.
   `https://koshvani-refresh.<you>.workers.dev`. Open `docs/index.html` and
   set:
   ```js
   const REFRESH_TRIGGER_URL = "https://koshvani-refresh.<you>.workers.dev/trigger";
   ```
   Commit and push.

Without this step, the dashboard still works fully — it just refreshes on
the twice-daily schedule, and clicking "Refresh" will point you to GitHub
Actions instead of triggering a live run itself.

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
python -m playwright install chromium

python scraper/list_grants.py            # lists every grant code + name
python scraper/list_schemes.py 011        # lists every scheme code + name under grant 011
```

Pick the grant code (e.g. `011`) and the numeric prefix of the scheme you
want (e.g. `2401000010500` from `2401000010500=जिला संगठन`), add a new object
to `schemes.json` with a unique `id`, commit, and push. The next scheduled
run (or a manual "Run workflow") will pick it up. You can also just paste the
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
  the scraper never hardcodes these; it always replays the real click path
  (main page -> Grant-wise expenditure -> grant -> scheme) in a fresh browser
  session, which is why each scheme takes a few seconds to scrape.
- If a scheme shows "No expenditure" or "No district rows", that reflects
  the portal itself having no recorded spend for that scheme/district in the
  current period — not a bug.
