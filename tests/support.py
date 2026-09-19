"""Shared fixtures for the Koshvani test suite.

Standard library only (unittest), fully offline: the portal, SMTP and Telegram
are always replaced by fakes, and every test works in its own temp directory,
so running the suite never touches docs/data, the real baseline or .env.

    python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scrape = _load("koshvani_scrape", REPO / "scraper" / "scrape.py")
alert = _load("koshvani_alert", REPO / "telegram_alert.py")

COLUMN_HEADERS = [
    "Treasury", "Standard Object", "Plan / Non-Plan", "Voted / Charged", "Progressive Allotment",
    "Actual Progressive Expenditure upto month (August)", "Provisional Current Month Expenditure(September)",
    "Total Expenditure Upto Month (September)", "% A/E",
]
LEGACY_INDEX_KEYS = {
    "id", "name", "grant_text", "scheme_code", "district", "status", "generated_at", "fin_year",
    "progressive_allotment", "total_expenditure", "pct_expenditure_of_allotment",
}


def make_schemes(n=24):
    return [{"id": f"agri-011-{2401000000000 + i}", "name": f"Scheme {i}", "grant_text": "011",
             "scheme_code": str(2401000000000 + i), "district": "G.B.NAGAR"} for i in range(1, n + 1)]


def ok_result(scheme, allot=1000.0, spent=500.0, month=0.0):
    """A valid scrape result, shaped exactly like the real scraper's output."""
    row = {"cells": ["G.B.NAGAR", "01-Pay", "N", "V", f"{allot:.2f}", f"{spent - month:.2f}", f"{month:.2f}",
                     f"{spent:.2f}", "50.00"]}
    return {**scrape.base_meta(scheme, "2026-2027"), "status": "ok", "message": None, "prev_month": "August",
            "current_month": "September", "column_headers": list(COLUMN_HEADERS), "column_widths": None,
            "rows": [row], "totals": scrape.compute_totals([row])}


class ScraperCase(unittest.TestCase):
    """Runs the real scrape.main() loop in a temp dir, with the portal replaced
    by a script: self.script[scheme_code] = [outcome, ...] where each outcome is
    a result dict (that attempt succeeds) or an exception (that attempt fails).
    Attempts beyond the script succeed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="koshvani_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.schemes = make_schemes()
        self._saved = {k: getattr(scrape, k) for k in ("DATA_DIR", "CONFIG_PATH", "_sleep", "attempt_scheme", "utc_now", "save_result")}
        self.addCleanup(self._restore)
        scrape.DATA_DIR = self.tmp / "data"
        scrape.CONFIG_PATH = self.tmp / "schemes.json"
        self.write_config(self.schemes)
        self.sleeps, self.calls, self.script, self.on_attempt = [], [], {}, None
        scrape._sleep = self.sleeps.append          # never really wait
        scrape.attempt_scheme = self.fake_attempt

    def _restore(self):
        for name, value in self._saved.items():
            setattr(scrape, name, value)

    def write_config(self, schemes):
        scrape.CONFIG_PATH.write_text(json.dumps(schemes), encoding="utf-8")

    def fake_attempt(self, scheme):
        code = scheme["scheme_code"]
        if self.on_attempt:
            self.on_attempt(scheme)
        n = self.calls.count(code)
        self.calls.append(code)
        outcomes = self.script.get(code, [])
        outcome = outcomes[n] if n < len(outcomes) else ok_result(scheme)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def run_main(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = scrape.main(install_signals=False)
        self.log = buf.getvalue()
        return rc

    def code(self, i):          # 1-based, like the log's "[SCHEME i/24]"
        return self.schemes[i - 1]["scheme_code"]

    def status(self):
        return json.loads((scrape.DATA_DIR / "crawler_status.json").read_text(encoding="utf-8"))

    def entry(self, i):
        return self.status()["schemes"][self.code(i)]

    def index(self):
        return json.loads((scrape.DATA_DIR / "index.json").read_text(encoding="utf-8"))

    def scheme_file(self, i):
        return json.loads((scrape.DATA_DIR / f"{self.schemes[i - 1]['id']}.json").read_text(encoding="utf-8"))
