#!/usr/bin/env python3
"""Sends the monitoring e-mail for the Koshvani daily crawler run (Gmail is the
only notification channel).

Two modes, called from run_daily.sh:

  python gmail_alert.py error --start <epoch> --stage git-pull|git-commit|git-push|log-push|scraper
      Job-level failure outside the scraper's own per-scheme handling
      (git operations, or the scraper process itself crashing).

  python gmail_alert.py report --start <epoch>
      Called after the scraper has run (whether it finished or was cut
      short). Reads the FINAL state of this execution from
      docs/data/crawler_status.json - after every scheme's retries - and
      e-mails a SUCCESS / WARNING / PARTIAL / ERROR report with the full
      per-scheme table. Financial values are compared with the
      .koshvani_previous_data/ baseline (ignoring generated_at).

The baseline only advances after the report was actually delivered (so a
failed send never silently loses change history), and even then only per
scheme - for schemes freshly scraped this run with healthy data - so one bad
run can neither lose change history nor corrupt a scheme's known-good
history (failed or unprocessed scheme). If Gmail is not configured at all,
nothing is being reported and the baseline simply advances.

Gmail needs GMAIL_SENDER, GMAIL_APP_PASSWORD and GMAIL_RECIPIENTS (comma
separated) in .env; without them the e-mail is skipped. An e-mail failure can
never fail the crawler. Every log line is sanitized (logsafe.py); the
password, the addresses and everything else in .env are never written out.
"""
import argparse
import json
import os
import shutil
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from html import escape as html_escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import logsafe  # noqa: E402  - the shared log sanitizer

ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "docs" / "data"
BASELINE_DIR = ROOT / ".koshvani_previous_data"
INDEX_PATH = DATA_DIR / "index.json"
SCHEMES_CONFIG_PATH = ROOT / "scraper" / "schemes.json"

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 465
SMTP_TIMEOUT = 30  # seconds; a stuck SMTP connection must never hang the daily job

# (cell index, human-readable label) for the four financial columns.
FINANCIAL_FIELDS = [
    (4, "Progressive Allotment"),
    (5, "Expenditure up to Previous Month"),
    (6, "Current Month Expenditure"),
    (7, "Total Expenditure up to Month"),
]

STAGE_MESSAGES = {
    "git-pull": "❌ Git pull failed.",
    "git-commit": "❌ Git commit failed.",
    "git-push": "❌ Git push failed.",
    "log-push": "❌ The execution log could not be pushed to GitHub (it stays committed locally and goes out with the next push).",
    "scraper": "❌ Scraper process failed to run (crashed before completing).",
}


# ---------- .env, logging ----------

def load_env(path=ENV_PATH):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _log(message, level="INFO"):
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    print(f"{stamp} {level:<5} [gmail_alert] {logsafe.scrub(message)}", file=sys.stderr)


def _redactions(env):
    """The mail credentials and addresses of THIS env, which must never reach a log line."""
    recipients = [r.strip() for r in (env.get("GMAIL_RECIPIENTS") or "").split(",")]
    password = env.get("GMAIL_APP_PASSWORD") or ""
    values = [password, "".join(password.split()), env.get("GMAIL_SENDER")] + recipients
    return sorted({v.strip() for v in values if v and len(v.strip()) >= 4}, key=len, reverse=True)


def _scrub(text, env=None):
    """Strips credentials and addresses from text before it is logged: the
    values of the given env explicitly, then everything logsafe knows about
    (.env values, tokens, cookies, encrypted query parameters, ...)."""
    if env is None:
        try:
            env = load_env()
        except Exception:
            env = {}
    text = str(text)
    for value in _redactions(env):
        text = text.replace(value, logsafe.REDACTED)
    return logsafe.scrub(text)


def _safely(label, fn, env):
    """Runs one delivery step; whatever goes wrong, log a sanitized reason and
    carry on - a notification problem must never stop the crawler."""
    try:
        return fn()
    except Exception as exc:
        _log(f"{label} step failed unexpectedly: {_scrub(type(exc).__name__ + ': ' + str(exc), env)}", "WARN")
        return False


# ---------- Gmail ----------

_LEVEL_COLORS = {"success": "#1a7f37", "warning": "#b45f06", "partial": "#6f42c1", "error": "#b3261e"}


def gmail_config(env):
    """(sender, app_password, recipients), or None when Gmail isn't configured.
    GMAIL_RECIPIENTS is comma separated; blanks and repeats are ignored."""
    sender = (env.get("GMAIL_SENDER") or "").strip()
    password = "".join((env.get("GMAIL_APP_PASSWORD") or "").split())  # Google shows it in spaced groups
    recipients, seen = [], set()
    for entry in (env.get("GMAIL_RECIPIENTS") or "").split(","):
        entry = entry.strip()
        if entry and "@" in entry and entry.lower() not in seen:
            seen.add(entry.lower())
            recipients.append(entry)
    if not (sender and password and recipients):
        return None
    return sender, password, recipients


def text_to_html(text, level):
    """A light HTML rendering of the SAME report text (nothing is recomputed,
    so the two channels can't drift). Inline styles only, no images, links or
    external assets; every piece of report text is escaped."""
    accent = _LEVEL_COLORS.get(level, "#444444")
    blocks = [b.split("\n") for b in text.strip().split("\n\n") if b.strip()]
    banner, body = "", []
    for i, lines in enumerate(blocks):
        if i == 0:
            banner = (f'<div style="background:{accent};color:#ffffff;padding:16px 20px;font-size:18px;'
                      f'font-weight:bold;border-radius:8px 8px 0 0">{html_escape(lines[0])}</div>')
            lines = lines[1:]
            if not lines:
                continue
        if lines[0].startswith("\u2022 "):
            detail = "<br>".join(html_escape(l.strip()) for l in lines[1:])
            body.append(f'<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;margin:14px 0">'
                        f'<div style="font-weight:bold">{html_escape(lines[0][2:])}</div>'
                        f'<div style="color:#4b5563;font-size:14px;line-height:1.5">{detail}</div></div>')
        elif i == len(blocks) - 1:
            body.append(f'<p style="margin:18px 0 0;font-weight:bold;color:{accent}">'
                        + "<br>".join(html_escape(l) for l in lines) + "</p>")
        else:
            body.append('<p style="margin:12px 0;line-height:1.6">' + "<br>".join(html_escape(l) for l in lines) + "</p>")
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            '<body style="margin:0;padding:16px;background:#f4f5f7">'
            '<div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e1e3e8;border-radius:8px;'
            'font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1f2328">'
            f'{banner}<div style="padding:4px 20px 20px">{"".join(body)}</div></div></body></html>')


def build_email(report, sender, recipients):
    """One message, addressed to every recipient: the report text is the
    plain-text part (complete on its own), with an HTML alternative."""
    msg = EmailMessage()
    msg["Subject"] = report["subject"]
    msg["From"] = formataddr(("Koshvani Alerts", sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(report["email_text"])
    msg.add_alternative(report["email_html"], subtype="html")
    return msg


def _ssl_context():
    # certifi ships with requests, so it is already present wherever the
    # scraper runs (including Termux, where the system CA store can be missing).
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _email_failure_reason(exc):
    """A short, credential-free reason - never str(exc) verbatim."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP authentication failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "all recipients were refused by the server"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "the sender was refused by the server"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"SMTP error {exc.smtp_code}"
    if isinstance(exc, smtplib.SMTPException):
        return f"SMTP error ({type(exc).__name__})"
    if isinstance(exc, ssl.SSLError):
        return "TLS/SSL error while connecting to Gmail"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "connection to Gmail timed out"
    if isinstance(exc, OSError):
        return f"could not reach Gmail SMTP ({type(exc).__name__})"
    return f"unexpected error ({type(exc).__name__})"


def send_email(report, env=None):
    """Gmail delivery. Never raises: any failure is logged (sanitized) and
    swallowed, so it cannot fail the crawler or touch the baseline. Returns
    True if Gmail accepted the message for at least one recipient."""
    env = load_env() if env is None else env
    try:
        config = gmail_config(env)
        if config is None:
            _log("Email notification skipped: Gmail is not configured.", "WARN")
            return False
        sender, password, recipients = config
        msg = build_email(report, sender, recipients)
        with smtplib.SMTP_SSL(GMAIL_HOST, GMAIL_PORT, context=_ssl_context(), timeout=SMTP_TIMEOUT) as server:
            server.login(sender, password)
            refused = server.send_message(msg, from_addr=sender, to_addrs=recipients)
        delivered = len(recipients) - len(refused)
        if refused:
            _log(f"Email notification sent to {delivered} of {len(recipients)} recipient(s); "
                 f"{len(refused)} refused by the server.", "WARN")
        else:
            _log(f"Email notification sent successfully to {delivered} recipient(s).")
        return delivered > 0
    except Exception as exc:
        _log(f"Email notification failed: {_scrub(_email_failure_reason(exc), env)}.", "WARN")
        return False


# ---------- helpers ----------

def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def now_str():
    return datetime.now().strftime("%d %b %Y, %I:%M %p")


def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_num(value):
    """Numeric-normalizes a scraped cell string so '0', '0.0', '0.00' compare
    equal, and so do '1000', '1,000', '1000.00'. Returns None if not numeric."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def format_num(value):
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def fmt_val(raw):
    n = parse_num(raw)
    return format_num(n) if n is not None else str(raw)


def nums_equal(a, b):
    na, nb = parse_num(a), parse_num(b)
    if na is None or nb is None:
        return str(a).strip() == str(b).strip()
    return abs(na - nb) < 0.005


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_configured_schemes():
    """The list of {id, name, ...} from scraper/schemes.json - the source of
    truth for which/how many schemes are expected, independent of whatever
    index.json happened to come out of this run. Returns None if it can't be
    read (scrape.py itself needs this file to run, so in practice this only
    happens if the crawler already failed before producing any results)."""
    schemes = load_json(SCHEMES_CONFIG_PATH)
    if not isinstance(schemes, list):
        return None
    return schemes


# ---------- row-level financial diff ----------

def row_key(row):
    """The first four fields (Treasury, Standard Object, Plan/Non-Plan,
    Voted/Charged) identify a row; duplicates are handled by occurrence
    index in index_rows(), not overwritten."""
    cells = row.get("cells", [])
    return tuple(cells[i] if i < len(cells) else "" for i in range(4))


def index_rows(rows):
    buckets = {}
    for row in rows:
        buckets.setdefault(row_key(row), []).append(row)
    return buckets


def diff_scheme_rows(prev_rows, curr_rows):
    """Returns (field_changes, structural_notes) comparing the 4 financial
    fields of matching rows by (identity key, occurrence index)."""
    prev_buckets = index_rows(prev_rows)
    curr_buckets = index_rows(curr_rows)
    field_changes = []
    structural_notes = []

    for key in set(prev_buckets) | set(curr_buckets):
        prev_list = prev_buckets.get(key, [])
        curr_list = curr_buckets.get(key, [])
        row_label = key[1] or key[0] or "row"
        for i in range(max(len(prev_list), len(curr_list))):
            prev_row = prev_list[i] if i < len(prev_list) else None
            curr_row = curr_list[i] if i < len(curr_list) else None
            if prev_row is None:
                structural_notes.append("New financial row detected")
                continue
            if curr_row is None:
                structural_notes.append("Financial row removed")
                continue
            prev_cells, curr_cells = prev_row.get("cells", []), curr_row.get("cells", [])
            for idx, field_name in FINANCIAL_FIELDS:
                old_val = prev_cells[idx] if idx < len(prev_cells) else None
                new_val = curr_cells[idx] if idx < len(curr_cells) else None
                if old_val is None or new_val is None or nums_equal(old_val, new_val):
                    continue
                old_n, new_n = parse_num(old_val), parse_num(new_val)
                diff = (new_n - old_n) if (old_n is not None and new_n is not None) else None
                field_changes.append({
                    "row_label": row_label, "field": field_name,
                    "old": old_val, "new": new_val, "diff": diff,
                })
    return field_changes, structural_notes


# ---------- report building ----------
# The report is ONE structured model of the FINAL state of the run (read from
# docs/data/crawler_status.json), rendered as a plain-text e-mail and an HTML
# e-mail that carries the full per-scheme table - both from the same facts.

LEVELS = {
    "success": ("🟢", "SUCCESS"),
    "warning": ("🟠", "WARNING"),
    "partial": ("🟣", "PARTIAL"),
    "error": ("🔴", "ERROR"),
}
FOOTERS = {
    "success": "✅ Job completed successfully.",
    "warning": "⚠️ Review required.",
    "partial": "⚠️ Execution incomplete. Review required.",
    "error": "❌ Job failed. Review required.",
}
STATUS_LEGEND = "Status legend: 1 = SUCCESS, 0 = NOT PROCESSED, -1 = FAILED"


def _headline(text):
    return text.split("\n", 1)[0]


def _status_path():
    return DATA_DIR / "crawler_status.json"


def _change_count(changes_by_scheme):
    return sum(len(fcs) for _, fcs in changes_by_scheme)


def _append_change_blocks(lines, changes_by_scheme):
    for name, fcs in changes_by_scheme:
        lines.append(f"• {name}")
        multi_row = len({fc["row_label"] for fc in fcs}) > 1
        for fc in fcs:
            prefix = f"[{fc['row_label']}] " if multi_row else ""
            lines.append(f"  {prefix}{fc['field']}: {fmt_val(fc['old'])} → {fmt_val(fc['new'])}")
            if fc["diff"] is not None:
                sign = "+" if fc["diff"] >= 0 else ""
                lines.append(f"  Change: {sign}{format_num(fc['diff'])}")
        lines.append("")


def _baseline_review(sid, detail):
    """Compares a freshly scraped scheme with its last-known-good baseline.
    Returns (review_note_or_None, field_changes)."""
    if detail is None:
        return "Result file missing or unreadable", []
    baseline_detail = load_json(BASELINE_DIR / f"{sid}.json")
    if baseline_detail is None:
        return None, []  # first run for this scheme - nothing to compare yet

    prev_status = baseline_detail.get("status")
    prev_rows = baseline_detail.get("rows") or []
    status = detail.get("status")
    curr_rows = detail.get("rows") or []

    if prev_status == "ok" and prev_rows and status != "ok":
        return "Previously had financial data; now empty — needs review", []
    if status == "ok" and prev_status == "ok":
        field_changes, structural_notes = diff_scheme_rows(prev_rows, curr_rows)
        if structural_notes:
            return "; ".join(dict.fromkeys(structural_notes)), field_changes
        return None, field_changes
    return None, []


def load_crawl_status(start_ts):
    """The status of THIS execution, or None. A file left by an earlier run
    must never be mistaken for it, so it has to have started after the job did."""
    crawl = load_json(_status_path())
    if not isinstance(crawl, dict) or not isinstance(crawl.get("schemes"), dict):
        return None
    try:
        started = datetime.fromisoformat(str(crawl["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if started.timestamp() < start_ts - 2:
        return None
    return crawl


def _scheme_table(crawl):
    """[{code, id, name}] in schemes.json order - the configuration, not the
    scraper's output, decides which schemes are expected."""
    configured = load_configured_schemes()
    if configured:
        return [{"code": str(s["scheme_code"]), "id": s["id"], "name": s.get("name") or s["id"]} for s in configured]
    index = load_json(INDEX_PATH) or {}
    from_index = [{"code": str(e.get("scheme_code")), "id": e.get("id"), "name": e.get("name") or e.get("id")}
                  for e in index.get("schemes", []) if isinstance(e, dict)]
    return from_index or [{"code": code, "id": None, "name": code} for code in crawl["schemes"]]


def _run_info():
    """(execution id, log file) of this run, handed over by run_daily.sh."""
    return os.environ.get("KOSHVANI_EXECUTION_ID", "").strip(), os.environ.get("KOSHVANI_LOG_FILE", "").strip()


def _job_error_report(ts, duration, reason):
    """A job-level failure (a git step, the scraper process): no per-scheme
    facts exist, so this is a short message."""
    execution_id, log_file = _run_info()
    info = "".join(f"\n{label}: {value}" for label, value in (("🆔 Execution ID", execution_id), ("📄 Log file", log_file)) if value)
    text = ("🔴 KOSHVANI CRAWLER — ERROR\n\n"
            f"⏱ {ts}\n⏳ Duration: {duration}{info}\n\n"
            f"{reason}\n\n"
            f"{FOOTERS['error']}")
    return {"kind": "job", "level": "error", "subject": _headline(text), "email_text": text,
            "email_html": text_to_html(text, "error"), "can_update_baseline": False, "fresh_ids": []}


def _remark_display(row):
    if row["remark"]:
        return row["remark"]
    if row["status"] == 1:
        return "Recovered on retry" if row["attempts"] > 1 else "Success"
    return "Not processed" if row["status"] == 0 else "Failed"


def render_email_text(m):
    """Plain-text alternative of the email: the full report, every scheme."""
    emoji, word = LEVELS[m["level"]]
    c = m["counts"]
    lines = [f"{emoji} KOSHVANI CRAWLER — {word}", "",
             f"Execution: {m['ts']}", f"Duration: {m['duration']}"]
    if m["execution_id"]:
        lines.append(f"Execution ID: {m['execution_id']}")
    if m["log_file"]:
        lines.append(f"Log file: {m['log_file']}")
    lines += ["",
              f"Schemes: {c['successful']}/{c['total']} successful",
              f"Total: {c['total']} | Successful: {c['successful']} | Failed: {c['failed']} | Unprocessed: {c['unprocessed']}",
              f"Recovered by retry: {c['recovered']}",
              f"Data changes: {_change_count(m['changes']) or 'None'}"]
    if m["fatal_error"]:
        lines.append(f"Fatal error: {m['fatal_error']}")
    lines += ["", f"ALL {c['total']} SCHEMES", "# | Scheme Code | Scheme Name | Status | Attempts | Remark"]
    lines += [f"{r['n']} | {r['code']} | {r['name']} | {r['status']} | {r['attempts']} | {_remark_display(r)}" for r in m["rows"]]
    lines += ["", STATUS_LEGEND, ""]
    if m["review"]:
        lines.append(f"DATA REVIEW NEEDED ({len(m['review'])})")
        for name, note in m["review"]:
            lines += [f"• {name}", f"  {note}", ""]
    if m["changes"]:
        lines += [f"DATA CHANGES ({_change_count(m['changes'])})", ""]
        _append_change_blocks(lines, m["changes"])
    lines.append(FOOTERS[m["level"]])
    return "\n".join(lines).strip() + "\n"


def render_email_html(m):
    """HTML alternative: summary, then the full table of every scheme. Inline
    styles only, no images/links/scripts; every dynamic value is escaped."""
    e = html_escape
    emoji, word = LEVELS[m["level"]]
    accent = _LEVEL_COLORS[m["level"]]
    c = m["counts"]
    n_changes = _change_count(m["changes"])

    def kv(label, value, bold=True):
        return (f'<tr><td style="padding:3px 18px 3px 0;color:#57606a">{e(label)}</td>'
                f'<td style="padding:3px 0;{"font-weight:bold" if bold else ""}">{value}</td></tr>')

    summary = "".join([
        kv("Execution", e(m["ts"])), kv("Duration", e(m["duration"])),
    ] + ([kv("Execution ID", e(m["execution_id"]))] if m["execution_id"] else [])
      + ([kv("Log file", e(m["log_file"]))] if m["log_file"] else []) + [
        kv("Schemes", f"{c['successful']}/{c['total']} successful"),
        kv("Total / Successful", f"{c['total']} / {c['successful']}"),
        kv("Failed", f"{c['failed']}"), kv("Unprocessed", f"{c['unprocessed']}"),
        kv("Recovered by retry", f"{c['recovered']}"), kv("Data changes", e(str(n_changes or "None"))),
    ] + ([kv("Fatal error", e(m["fatal_error"]))] if m["fatal_error"] else []))

    status_color = {1: "#1a7f37", 0: "#6b7280", -1: "#b3261e"}
    status_bg = {1: "", 0: "background:#f3f4f6;", -1: "background:#fdecea;"}
    cell = "padding:7px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px"
    head = "".join(
        f'<th style="text-align:left;padding:8px 10px;background:#f3f4f6;font-size:12px;color:#374151;'
        f'border-bottom:2px solid #d1d5db;white-space:nowrap">{h}</th>'
        for h in ("#", "Scheme Code", "Scheme Name", "Status", "Attempts", "Remark"))
    body = []
    for r in m["rows"]:
        retried = r["status"] == 1 and r["attempts"] > 1
        body.append(
            f'<tr style="{status_bg[r["status"]]}">'
            f'<td style="{cell}">{r["n"]}</td>'
            f'<td style="{cell};white-space:nowrap">{e(r["code"])}</td>'
            f'<td style="{cell}">{e(r["name"])}</td>'
            f'<td style="{cell};white-space:nowrap"><b style="color:{status_color[r["status"]]}">{r["status"]}</b></td>'
            f'<td style="{cell};{"background:#fff4e5;font-weight:bold" if retried else ""}">{r["attempts"]}</td>'
            f'<td style="{cell};color:{"#b3261e" if r["status"] == -1 else "#374151"}">{e(_remark_display(r))}</td></tr>')
    table = (f'<div style="overflow-x:auto"><table role="presentation" cellspacing="0" cellpadding="0" '
             f'style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb"><thead><tr>{head}</tr></thead>'
             f'<tbody>{"".join(body)}</tbody></table></div>')

    def card(title, lines):
        detail = "<br>".join(e(l) for l in lines)
        return (f'<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;margin:12px 0">'
                f'<div style="font-weight:bold">{e(title)}</div><div style="color:#4b5563;font-size:14px;line-height:1.5">{detail}</div></div>')

    sections = []
    if m["review"]:
        sections.append(f'<h3 style="font-size:15px;margin:22px 0 6px">Data review needed ({len(m["review"])})</h3>'
                        + "".join(card(name, [note]) for name, note in m["review"]))
    if m["changes"]:
        cards = []
        for name, fcs in m["changes"]:
            multi_row = len({fc["row_label"] for fc in fcs}) > 1
            lines = []
            for fc in fcs:
                prefix = f"[{fc['row_label']}] " if multi_row else ""
                lines.append(f"{prefix}{fc['field']}: {fmt_val(fc['old'])} → {fmt_val(fc['new'])}")
                if fc["diff"] is not None:
                    lines.append(f"Change: {'+' if fc['diff'] >= 0 else ''}{format_num(fc['diff'])}")
            cards.append(card(name, lines))
        sections.append(f'<h3 style="font-size:15px;margin:22px 0 6px">Data changes ({n_changes})</h3>' + "".join(cards))

    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            '<body style="margin:0;padding:16px;background:#f4f5f7">'
            '<div style="max-width:760px;margin:0 auto;background:#ffffff;border:1px solid #e1e3e8;border-radius:8px;'
            'font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1f2328">'
            f'<div style="background:{accent};color:#ffffff;padding:16px 20px;font-size:18px;font-weight:bold;'
            f'border-radius:8px 8px 0 0">{e(emoji)} KOSHVANI CRAWLER — {e(word)}</div>'
            f'<div style="padding:14px 20px 20px"><table role="presentation" cellspacing="0" cellpadding="0" '
            f'style="margin-bottom:18px;font-size:14px">{summary}</table>'
            f'<h3 style="font-size:15px;margin:0 0 8px">All {c["total"]} schemes</h3>{table}'
            f'<p style="margin:8px 0 0;font-size:12px;color:#6b7280">{e(STATUS_LEGEND)}</p>'
            f'{"".join(sections)}'
            f'<p style="margin:20px 0 0;font-weight:bold;color:{accent}">{e(FOOTERS[m["level"]])}</p></div></div></body></html>')


def build_report(start_ts):
    duration = format_duration(time.time() - start_ts)
    ts = now_str()

    crawl = load_crawl_status(start_ts)
    if crawl is None:
        return _job_error_report(
            ts, duration,
            "❌ The scraper left no status for this run (crawler_status.json is missing, unreadable or from an earlier run).")
    fatal = str(crawl.get("fatal_error") or "").strip()

    rows, fresh_ids, changes, review = [], [], [], []
    for n, scheme in enumerate(_scheme_table(crawl), 1):
        entry = crawl["schemes"].get(scheme["code"])
        if not isinstance(entry, dict) or entry.get("status") not in (1, 0, -1):
            entry = {"status": 0, "attempts": 0, "remark": "Missing from crawler status"}
        row = {"n": n, "code": scheme["code"], "name": scheme["name"], "status": entry["status"],
               "attempts": int(entry.get("attempts") or 0), "remark": str(entry.get("remark") or "")}
        rows.append(row)
        if row["status"] == 1 and scheme["id"]:
            fresh_ids.append(scheme["id"])
            note, field_changes = _baseline_review(scheme["id"], load_json(DATA_DIR / f"{scheme['id']}.json"))
            if note:
                review.append((scheme["name"], note))
            if field_changes:
                changes.append((scheme["name"], field_changes))
    if not rows:
        return _job_error_report(ts, duration, f"❌ {fatal or 'No schemes were tracked for this run.'}")

    counts = {
        "total": len(rows),
        "successful": sum(1 for r in rows if r["status"] == 1),
        "failed": sum(1 for r in rows if r["status"] == -1),
        "unprocessed": sum(1 for r in rows if r["status"] == 0),
        "recovered": sum(1 for r in rows if r["status"] == 1 and r["attempts"] > 1),
    }
    # Final level, from the FINAL per-scheme states (retries already applied):
    if counts["unprocessed"]:
        fatal = fatal or "The crawler stopped before every scheme was processed (interrupted or killed)."
        level = "error" if counts["successful"] + counts["failed"] == 0 else "partial"
    elif counts["failed"] or review:
        level = "warning"
    else:
        level = "success"

    execution_id, log_file = _run_info()
    m = {"level": level, "ts": ts, "duration": duration, "counts": counts, "rows": rows,
         "changes": changes, "review": review, "fatal_error": fatal,
         "execution_id": execution_id or str(crawl.get("execution_id") or ""), "log_file": log_file}
    emoji, word = LEVELS[level]
    subject = f"{emoji} KOSHVANI CRAWLER — {word}"
    if level != "error":
        subject += f" | {counts['successful']}/{counts['total']} Schemes"
    return {"kind": "crawl", "level": level, "subject": subject,
            "email_text": render_email_text(m), "email_html": render_email_html(m),
            "can_update_baseline": level != "error", "fresh_ids": fresh_ids, "counts": counts, "model": m}


def build_error_report(start_ts, stage):
    """Job-level failure (a git step, the scraper process) - no per-scheme counts exist."""
    duration = format_duration(time.time() - start_ts)
    return _job_error_report(now_str(), duration, STAGE_MESSAGES.get(stage, f"❌ {stage} failed."))


def _fresh_ids_from_status():
    crawl = load_json(_status_path()) or {}
    entries = crawl.get("schemes") if isinstance(crawl.get("schemes"), dict) else {}
    return [s["id"] for s in _scheme_table({"schemes": entries})
            if s["id"] and isinstance(entries.get(s["code"]), dict) and entries[s["code"]].get("status") == 1]


def update_baseline(fresh_ids=None):
    """Per-scheme baseline update: a scheme's baseline file is only replaced
    when the scheme was freshly scraped in THIS execution (crawl status 1) and
    its data is healthy (status "ok"). A scheme that failed, was never
    processed (its file on disk is last run's), came back empty or hit an
    unexpected status keeps its last known-good baseline untouched."""
    if fresh_ids is None:
        fresh_ids = _fresh_ids_from_status()

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for sid in fresh_ids:
        src = DATA_DIR / f"{sid}.json"
        detail = load_json(src)
        if not detail or detail.get("status") != "ok":
            continue
        tmp = BASELINE_DIR / f"{sid}.json.tmp"
        shutil.copyfile(src, tmp)
        tmp.replace(BASELINE_DIR / f"{sid}.json")

    # index.json is just a manifest (not per-scheme financial data) - always refresh it.
    if INDEX_PATH.exists():
        tmp = BASELINE_DIR / "index.json.tmp"
        shutil.copyfile(INDEX_PATH, tmp)
        tmp.replace(BASELINE_DIR / "index.json")


# ---------- CLI ----------

def cmd_report(args):
    env = load_env()
    report = build_report(args.start)
    configured = gmail_config(env) is not None

    delivered = _safely("Email", lambda: send_email(report, env), env)

    # Gmail is the only channel, so it gates the baseline: the baseline only
    # advances once the report (and with it every change since the last
    # baseline) has really been delivered, so a failed send never silently loses
    # change history. With no Gmail configured nothing is reported at all, so
    # there is nothing to protect and the baseline just advances.
    if delivered or not configured:
        if report["can_update_baseline"]:
            _safely("Baseline update", lambda: update_baseline(report.get("fresh_ids")), env)
        _log(f"Report {'e-mailed' if delivered else 'built (Gmail not configured - nothing to deliver)'} ({report['level']}).")
    else:
        _log("Email not delivered; baseline NOT updated so changes stay detectable next run.", "WARN")


def cmd_error(args):
    env = load_env()
    report = build_error_report(args.start, args.stage)
    _safely("Email", lambda: send_email(report, env), env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Validate results, compare baseline, send SUCCESS/WARNING/ERROR")
    p_report.add_argument("--start", type=float, required=True, help="Unix timestamp when the job started")
    p_report.set_defaults(func=cmd_report)

    p_error = sub.add_parser("error", help="Send a concise ERROR alert for a job-level failure")
    p_error.add_argument("--start", type=float, required=True)
    p_error.add_argument("--stage", required=True, choices=list(STAGE_MESSAGES))
    p_error.set_defaults(func=cmd_error)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:  # monitoring must never take the crawler job down with it
        _log(f"Unexpected error in monitoring: {_scrub(exc)}", "ERROR")


if __name__ == "__main__":
    main()
