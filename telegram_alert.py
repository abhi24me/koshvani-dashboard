#!/usr/bin/env python3
"""Sends concise Telegram monitoring alerts for the Koshvani daily crawler run.

Two modes, called from run_daily.sh:

  python telegram_alert.py error --start <epoch> --stage git-pull|git-commit|git-push|scraper
      Job-level failure outside the scraper's own per-scheme handling
      (git operations, or the scraper process itself crashing).

  python telegram_alert.py report --start <epoch>
      Called after a successful scraper run. Validates the configured
      scrape/schemes.json schemes against docs/data/index.json (catching
      missing/extra/duplicate entries), compares financial values against
      the .koshvani_previous_data/ baseline (ignoring generated_at), and
      sends a SUCCESS/WARNING/ERROR summary. The baseline is only refreshed
      if the Telegram send succeeds, and even then only per scheme - a
      scheme's baseline file is replaced solely when its current result is
      healthy (status "ok"), so one bad run can neither lose change history
      (failed send) nor corrupt a scheme's known-good history (failed run).

Never prints or logs TELEGRAM_BOT_TOKEN.
"""
import argparse
import json
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "docs" / "data"
BASELINE_DIR = ROOT / ".koshvani_previous_data"
INDEX_PATH = DATA_DIR / "index.json"
SCHEMES_CONFIG_PATH = ROOT / "scraper" / "schemes.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MAX_LEN = 4000  # a little under Telegram's 4096 hard limit, for safety

# (cell index, human-readable label) for the four financial columns.
FINANCIAL_FIELDS = [
    (4, "Progressive Allotment"),
    (5, "Expenditure up to Previous Month"),
    (6, "Current Month Expenditure"),
    (7, "Total Expenditure up to Month"),
]
KNOWN_STATUSES = {"ok", "empty", "no_district_data", "error"}

STAGE_MESSAGES = {
    "git-pull": "❌ Git pull failed.",
    "git-commit": "❌ Git commit failed.",
    "git-push": "❌ Git push failed.",
    "scraper": "❌ Scraper process failed to run (crashed before completing).",
}


# ---------- .env / Telegram send ----------

def load_env(path=ENV_PATH):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def split_message(text, limit=TELEGRAM_MAX_LEN):
    """Splits on blank-line block boundaries so a scheme's block is never cut
    in half; only hard-splits if a single block is somehow still too long."""
    if len(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    parts, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    final = []
    for p in parts:
        if len(p) <= limit:
            final.append(p)
        else:
            final.extend(p[i:i + limit] for i in range(0, len(p), limit))
    return final


def send_telegram(text, env=None):
    env = env or load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram_alert] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured; skipping send.", file=sys.stderr)
        return False
    url = TELEGRAM_API.format(token=token)
    for chunk in split_message(text):
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=20)
            if resp.status_code != 200:
                print(f"[telegram_alert] Telegram API returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return False
        except requests.exceptions.RequestException as exc:
            print(f"[telegram_alert] Telegram send failed: {exc}", file=sys.stderr)
            return False
    return True


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

def _append_changes_section(lines, changes_by_scheme):
    total_changes = sum(len(fc) for _, fc in changes_by_scheme)
    lines.append(f"📈 Data changes: {total_changes}")
    lines.append("")
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


def _evaluate_scheme(sid, name, entry, detail):
    """Checks one configured, present-in-results scheme against its baseline.
    Returns (problem_or_None, field_changes)."""
    status = entry.get("status")
    if detail is None:
        return "Result file missing or unreadable", []
    if status == "error":
        return truncate(detail.get("message") or "Unknown scraper error", 140), []
    if status not in KNOWN_STATUSES:
        return f"Unexpected status: {status}", []

    # status is "ok" / "empty" / "no_district_data" - a legitimate,
    # non-error scraper outcome. Compare against the baseline if we have one.
    baseline_detail = load_json(BASELINE_DIR / f"{sid}.json")
    if baseline_detail is None:
        return None, []  # first run for this scheme - nothing to compare yet

    prev_status = baseline_detail.get("status")
    prev_rows = baseline_detail.get("rows") or []
    curr_rows = detail.get("rows") or []

    if prev_status == "ok" and prev_rows and status != "ok":
        return "Previously had financial data; now empty — needs review", []

    if status == "ok" and prev_status == "ok":
        field_changes, structural_notes = diff_scheme_rows(prev_rows, curr_rows)
        if structural_notes:
            return "; ".join(dict.fromkeys(structural_notes)), field_changes
        return None, field_changes

    return None, []


def build_report(start_ts):
    duration = format_duration(time.time() - start_ts)
    ts = now_str()

    index = load_json(INDEX_PATH)
    if not index or "schemes" not in index:
        text = (
            "🔴 KOSHVANI CRAWLER — ERROR\n\n"
            f"⏱ {ts}\n⏳ Duration: {duration}\n\n"
            "❌ Scraper completed but produced no readable results (index.json missing/corrupt).\n\n"
            "❌ Job failed. Review required."
        )
        return {"text": text, "level": "error", "can_update_baseline": False}

    schemes_index = index["schemes"]

    # scraper/schemes.json (not len(index["schemes"])) is the source of truth
    # for how many schemes are expected - so a run that silently drops a
    # configured scheme is caught instead of reporting e.g. "23/23".
    configured = load_configured_schemes()
    configured_by_id = {s["id"]: s for s in configured} if configured else None

    by_id = {}
    duplicate_ids = set()
    for entry in schemes_index:
        sid = entry.get("id")
        if sid in by_id:
            duplicate_ids.add(sid)
        by_id[sid] = entry

    if configured_by_id is not None:
        expected_ids = list(configured_by_id.keys())
        dup_configured = {sid for sid, count in Counter(s["id"] for s in configured).items() if count > 1}
    else:
        expected_ids = [entry.get("id") for entry in schemes_index]
        dup_configured = set()
    total = len(expected_ids)

    attention = []          # [(name, concise problem)] - shown in the message
    changes_by_scheme = []  # [(name, [field_change, ...])]
    configured_issues = 0   # subset of `attention` that counts against `total`

    for sid in dup_configured:
        name = configured_by_id.get(sid, {}).get("name", sid)
        attention.append((name, "Configured multiple times in scraper/schemes.json"))
        configured_issues += 1

    for sid in expected_ids:
        entry = by_id.get(sid)
        name = (configured_by_id.get(sid, {}).get("name") if configured_by_id else None) \
            or (entry.get("name") if entry else sid)

        if entry is None:
            attention.append((name, "Missing from generated results"))
            configured_issues += 1
            continue
        if sid in duplicate_ids:
            attention.append((name, "Duplicate entry in generated results"))
            configured_issues += 1
            continue

        detail = load_json(DATA_DIR / f"{sid}.json")
        problem, field_changes = _evaluate_scheme(sid, name, entry, detail)
        if problem:
            attention.append((name, problem))
            configured_issues += 1
        if field_changes:
            changes_by_scheme.append((name, field_changes))

    # Schemes present in the generated results but not part of the configured
    # 24 - flagged, but they don't count against the configured total.
    if configured_by_id is not None:
        for entry in schemes_index:
            sid = entry.get("id")
            if sid not in configured_by_id:
                attention.append((entry.get("name", sid), "Unexpected scheme not in schemes.json configuration"))

    successful = total - configured_issues
    level = "warning" if attention else "success"

    lines = []
    if level == "success":
        lines += ["🟢 KOSHVANI CRAWLER — SUCCESS", "", f"⏱ {ts}", f"⏳ Duration: {duration}", "",
                  f"📊 Schemes: {successful}/{total} successful"]
        if not changes_by_scheme:
            lines.append("📈 Data changes: None")
        else:
            _append_changes_section(lines, changes_by_scheme)
        lines.append("✅ Job completed successfully.")
    else:  # level == "warning"
        lines += ["🟠 KOSHVANI CRAWLER — WARNING", "", f"⏱ {ts}", f"⏳ Duration: {duration}", "",
                  f"📊 Schemes: {successful}/{total} successful",
                  f"⚠️ {len(attention)} scheme(s) need attention", ""]
        for name, problem in attention:
            lines += [f"• {name}", f"  {problem}", ""]
        if changes_by_scheme:
            _append_changes_section(lines, changes_by_scheme)
        lines.append("⚠️ Review required.")

    text = "\n".join(lines).strip() + "\n"
    return {"text": text, "level": level, "can_update_baseline": True}


def update_baseline():
    """Per-scheme baseline update: a scheme's baseline file is only replaced
    when its current result is healthy (status == "ok"). A scheme that
    failed/came back empty/hit an unexpected status keeps its last
    known-good baseline untouched, so one bad run can't wipe out valid
    history - next time it succeeds, its baseline catches up again."""
    index = load_json(INDEX_PATH)
    schemes_index = index.get("schemes", []) if index else []

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for entry in schemes_index:
        if entry.get("status") != "ok":
            continue
        sid = entry.get("id")
        src = DATA_DIR / f"{sid}.json"
        if not src.exists():
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
    report = build_report(args.start)
    sent = send_telegram(report["text"])
    if not sent:
        print("[telegram_alert] Telegram send failed; baseline NOT updated so changes stay detectable next run.",
              file=sys.stderr)
        return
    if report["can_update_baseline"]:
        update_baseline()
    print(f"[telegram_alert] Report sent ({report['level']}).", file=sys.stderr)


def cmd_error(args):
    duration = format_duration(time.time() - args.start)
    reason = STAGE_MESSAGES.get(args.stage, f"❌ {args.stage} failed.")
    text = (
        "🔴 KOSHVANI CRAWLER — ERROR\n\n"
        f"⏱ {now_str()}\n⏳ Duration: {duration}\n\n"
        f"{reason}\n\n"
        "❌ Job failed. Review required."
    )
    send_telegram(text)


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
        print(f"[telegram_alert] Unexpected error in monitoring: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
