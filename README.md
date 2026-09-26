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
commit rather than discarded. Every run also writes, commits and pushes its own
[execution log](#execution-logs), and mails a report through
[Gmail](#notifications-gmail-only):

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

- **Concurrent, but isolated.** Schemes are processed by a
  `ThreadPoolExecutor` with `MAX_WORKERS = 4` workers (top of
  `scraper/scrape.py`; set it to 1 to get the old strictly sequential
  behaviour, raise it cautiously - the portal is flaky even with one client).
  Every attempt builds its own `requests.Session` with its own TLS adapter, so
  no cookies, connection pools or portal links are ever shared between schemes.
  Workers only touch their own scheme's file and the (locked) status; a fatal
  error or a kill in any worker stops the run: in-flight schemes finish their
  current attempt, queued ones are dropped and stay at status 0 (`PARTIAL`).
- **Staggered, and polite when the portal is unwell.** No two attempts - of
  any worker, retries included - start within `STAGGER_SECONDS` (4 s, plus up
  to `STAGGER_JITTER` = 2 s random) of each other, so the workers never hit the
  portal in the same second. The execution logs showed the portal bouncing
  sessions to `ClearSession.aspx` in bursts that hit every session at once
  (mostly early morning): when a response is such a bounce, **all** workers
  hold for `BOUNCE_HOLD_MIN`..`MAX` (60-120 s) before their next attempt,
  instead of each retrying on its own short timer.
- **Cool-down rounds.** Retry waits of 5-30 s cannot outlast a bad spell that
  lasts minutes. So after the first pass, every scheme that still failed gets up
  to `COOLDOWN_ROUNDS` (2) more rounds, each after a `COOLDOWN_WAIT` (300 s)
  pause and each a full 3-attempt turn. The status file shows the running
  total (`attempts` up to 9) and a scheme saved this way is remarked `Recovered
  in cool-down round N`. Skipped after a fatal error or a stop request. Set
  `COOLDOWN_ROUNDS = 0` and `STAGGER_SECONDS = 0` to switch both off.
- **Last good data is kept.** A scheme that fails every attempt (and every
  cool-down round) does **not** overwrite its data file if that file already
  holds real data from an earlier run: the dashboard keeps showing the last good
  numbers (with their old `generated_at`), while `crawler_status.json` still
  records `-1` and the error, `index.json` marks it (`crawl_status: -1`), the
  report counts it as failed and the baseline does not advance for it. Only a
  scheme with nothing good on disk gets the "Fetch error" stub.
- **Per-scheme retries, in the same run.** Each scheme gets up to 3 attempts
  (`MAX_ATTEMPTS`) inside its own worker turn - there is no separate retry pass
  afterwards, and a scheme that succeeded is never retried. Every attempt uses
  a fresh session (fresh cookies and freshly generated portal links). The wait
  before attempt 2 is random 5-15 s and before attempt 3 random 15-30 s
  (`RETRY_1_DELAY_*`, `RETRY_2_DELAY_*` at the top of `scraper/scrape.py`); it
  only holds up the worker that needs it (unless the portal is bouncing, see
  above).
- **Saved immediately.** A scheme's `docs/data/<id>.json` is written (atomically:
  unique temp file, `fsync`, JSON re-validated, then `os.replace`) the moment
  it has been scraped and validated, so a later failure or crash can never take
  it away. `crawler_status.json` is updated under a lock and written the same
  atomic way, so every reader always sees a complete file. `index.json` is only
  ever written by the main thread, from a consistent snapshot, each time a
  scheme finishes and once more at the end - so it never claims more than what
  is really on disk.
- **`docs/data/crawler_status.json`** - exactly one file, describing the
  *current* execution only (git history keeps the previous ones), reset at the
  start of every run. Keyed by the numeric scheme code from `schemes.json`:

  ```json
  {
    "execution_id": "20260919-143012-9a80e1",
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
  attempt of the last cool-down round it stays -1 with `attempts: 9` and the
  latest error), **0** = never
  processed (only possible if the run was cut short, which makes the overall
  status `PARTIAL`). It never stores links,
  cookies or sessions; URLs in error messages are reduced to the page name.
- **Overall status:** `SUCCESS` (all 24 = 1), `WARNING` (all attempted, some
  -1), `PARTIAL` (cut short by a fatal error or a kill - some still 0),
  `ERROR` (nothing could be attempted). The scraper exits 0 for the first two
  and 3 otherwise; either way everything it saved is kept.
- **Reports.** The Gmail report is generated from this *final* state, after
  the retries: a scheme that failed once and then recovered is not a failure.
  It carries the full 24-scheme table (status, attempts, remark for every
  scheme), the execution id and the name of the execution log.
- **Baseline.** `.koshvani_previous_data/` still advances per scheme, only for
  schemes freshly scraped in that run with healthy data, and - since Gmail is
  now the only channel - only once the report e-mail was actually delivered
  (or when Gmail is not configured at all, when there is nothing to protect).
  A failed send therefore never loses change history: the same changes are
  reported by the next run. (Updating it inside the scraper would erase the
  before/after that "Data changes" needs.)

## Notifications (Gmail only)

`gmail_alert.py` is the only notification mechanism. Put these in a local
`.env` (never committed - it is in `.gitignore`):

```
GMAIL_SENDER=you@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx     # a Google "app password", not your login password
GMAIL_RECIPIENTS=you@gmail.com,someone@example.com
```

`run_daily.sh` calls it once per run, after the data has been pushed:
`python gmail_alert.py report` (SUCCESS / WARNING / PARTIAL / ERROR, from the
final state) or `python gmail_alert.py error --stage git-pull|git-commit|git-push|log-push`
for a job-level failure. Without those three variables no mail is sent; a mail
problem can never fail the crawler or touch the data.

## Execution logs

Every run of `run_daily.sh` creates **its own log**, never reused and never
edited afterwards: `logs/daily-YYYY-MM-DD-HHMMSS.log` (a second run in the same
second gets a `-2` suffix). Whatever the run does - a full success, a partial
run, no data changes, a failed pull - the finished log is committed
(`Add execution log <execution_id>`) and pushed to `main`, so the history of
every run lives in git. Logs a run could not push (no network) go out with the
next run.

A log records, with millisecond timestamps, the level and the worker thread:

- the unique **execution id** (`YYYYMMDD-HHMMSS-xxxxxx`), also written to
  `crawler_status.json`, `index.json` and the Gmail report;
- per scheme: which worker took it, every attempt, the retry waits, the total
  time and the final status; and for every HTTP request its start, end, status,
  size and elapsed time, or the failure kind (`timeout`, `connection-reset`,
  `ssl`, `http-503`, `validation`, ...);
- parsing and validation results, every file written (bytes, milliseconds),
  every `crawler_status.json` update;
- a performance summary: wall time, worker utilisation, slowest schemes,
  request-latency average / p95 / max, failures by kind, time spent in retry
  waits, time spent in start staggers / portal holds, and how many portal
  bounces were seen;
- every Git operation with its exit code and duration, the result of the Gmail
  step, and the final execution status.

**Nothing sensitive is ever written.** All output passes through
[logsafe.py](logsafe.py) (standard library only): cookies, `Authorization`
headers, tokens, API keys, passwords, session ids, the portal's encrypted URL
query parameters (URLs are logged as page names only), long opaque tokens,
e-mail addresses and every value found in `.env` are replaced by `<redacted>`.
It runs in three places: as the logging formatter of the scraper and the mailer,
as a filter between `run_daily.sh` and the log file, and as a last check
(`logsafe.py --scrub-file`, `--check`) on each log right before it is committed
- a log that would still change under the sanitizer is not published. Only
explicit paths are ever staged (`docs/data` and the execution logs), never the
whole tree. The last lines of a log announce the publish step; what that step
itself prints (it cannot be part of the file it commits) only goes to the
screen.

## Running the tests

```
python -m unittest discover -s tests -v
```

Fully offline (fake portal and SMTP; every test works in a temp directory, so
the suite never touches `docs/data`, `logs/`, the real baseline or `.env`). It
covers the retry and status semantics, the thread pool (real parallelism, the
`MAX_WORKERS` bound, isolation, atomic files under concurrent readers), the log
format, the sanitizer, the Gmail reports and the baseline gate, and
`run_daily.sh` in a sandbox git repo with a bare `origin` (about six minutes,
most of it process start-up on Windows). Those need `bash` and `git`, and the
dashboard tests need Playwright; each skips itself when unavailable.

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
