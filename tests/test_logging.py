"""The execution log: what it says about every attempt, request and file, that the
scraping itself is unchanged, and - above all - that nothing secret can be in it."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import COLUMN_HEADERS, ScraperCase, ok_result, scrape  # noqa: E402
import logsafe  # noqa: E402

TIMEOUT = requests.exceptions.ReadTimeout("Read timed out. (read timeout=45)")
RESET = requests.exceptions.ConnectionError("Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))

LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} (INFO |WARNING|ERROR) \[[\w-]+\] ")


class FakeResponse:
    def __init__(self, url, body, status=200, history=()):
        self.url, self.text, self.status_code, self.history = url, body, status, list(history)
        self.content = body.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Server Error for url: {self.url}", response=self)


SECRET_QUERY = "enc=Zk9x3QvT8rLm2Pq7WnYb4Hc6JdSa1UeVgXi0RoNt5AwBfKyMlCzDhEjIpG"


def page(name, body):
    return f"https://koshvani.up.nic.in/Reports/{name}?{SECRET_QUERY}", f"<html><head><title>{name}</title></head><body>{body}</body></html>"


def report_table(district="G.B.NAGAR", with_header=True):
    head = "".join(f'<th style="width:{w}%">{h}</th>' for h, w in zip(COLUMN_HEADERS, [11] * 9)) if with_header else ""
    rows = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>" for cells in (
            [district, "01-Pay", "N", "V", "1,000.00", "400.00", "100.00", "500.00", "50.00"],
            ["", "02-Allowance", "N", "V", "200.00", "50.00", "10.00", "60.00", "30.00"]))
    return f'<table id="Table1"><tr>{head}</tr></table><table id="myTable">{rows}</table>'


class FakePortal:
    """Stands in for requests.Session: serves the four pages of the portal's click path."""

    variant = "ok"          # ok | no_record | no_header | timeout | reset
    log = []

    def __init__(self):
        self.headers, self.adapters, self.closed = {}, {}, False

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def close(self):
        self.closed = True

    def get(self, url, timeout=None):
        name = url.split("?")[0].rsplit("/", 1)[-1]
        FakePortal.log.append(name)
        if FakePortal.variant == "timeout" and name == "ExpHead.aspx":
            raise TIMEOUT
        if FakePortal.variant == "reset" and name == "ExpHead.aspx":
            raise RESET
        if name == "KoshvaniStatic.aspx":
            u, b = page(name, '<select id="ddlFinYear"><option selected>2026-2027</option></select>'
                              f'<a href="Reports/ExpGrant.aspx?{SECRET_QUERY}">Grant-wise expenditure</a>')
            return FakeResponse(u, b)
        if name == "ExpGrant.aspx":
            u, b = page(name, f'<select id="ddlAmountIn"></select><table><tr><td><a href="ExpHead.aspx?{SECRET_QUERY}">011</a></td></tr></table>')
            return FakeResponse(u, b)
        if name == "ExpHead.aspx":
            target = "NoRecordFound.htm" if FakePortal.variant == "no_record" else "ExpTreas.aspx"
            u, b = page(name, f'<table><tr><td><a href="{target}?{SECRET_QUERY}">2401000000001 Scheme 1</a></td></tr></table>')
            return FakeResponse(u, b)
        if name == "NoRecordFound.htm":
            u, b = page(name, "<p>No record found</p>")
            return FakeResponse(u, b)
        if name == "ExpTreas.aspx":
            u, b = page(name, report_table(with_header=FakePortal.variant != "no_header"))
            return FakeResponse(u, b)
        raise AssertionError(f"unexpected URL {url}")


class LogCase(ScraperCase):
    def setUp(self):
        super().setUp()
        # a private, throwaway .env so the tests are independent of the developer's real one
        self.env_dir = tempfile.mkdtemp(prefix="koshvani_env_")
        self.addCleanup(__import__("shutil").rmtree, self.env_dir, True)
        self.env_file = Path(self.env_dir) / ".env"
        self.env_file.write_text("GMAIL_SENDER=alerts.sender@example.org\nGMAIL_APP_PASSWORD=abcd efgh ijkl mnop\n"
                                 "GMAIL_RECIPIENTS=boss@example.org,team@example.org\nSOME_TOKEN=tok_live_0123456789abcdef\n",
                                 encoding="utf-8")
        logsafe.configure(self.env_file)
        self.addCleanup(logsafe.configure, None)

    def lines(self, pattern):
        return [l for l in self.log.splitlines() if re.search(pattern, l)]


class TestExecutionLog(LogCase):
    workers = 4

    def test_every_line_has_a_timestamp_level_and_thread(self):
        self.run_main()
        bad = [l for l in self.log.splitlines() if l and not LINE.match(l)]
        self.assertEqual(bad, [])

    def test_the_execution_id_is_logged_and_matches_the_status_file(self):
        self.run_main()
        eid = self.status()["execution_id"]
        self.assertRegex(eid, r"^\d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(len(self.lines(rf"Execution ID: {eid}")), 2, "once at the start, once in the summary")
        self.assertEqual(self.index()["execution_id"], eid)

    def test_the_execution_id_handed_over_by_the_runner_is_used(self):
        with mock.patch.dict("os.environ", {"KOSHVANI_EXECUTION_ID": "20260922-010203-abc123"}):
            self.run_main()
        self.assertEqual(self.status()["execution_id"], "20260922-010203-abc123")
        self.assertIn("Execution ID: 20260922-010203-abc123", self.log)

    def test_two_runs_get_two_ids(self):
        self.run_main()
        one = self.status()["execution_id"]
        self.run_main()
        self.assertNotEqual(one, self.status()["execution_id"])

    def test_every_scheme_has_start_attempt_and_done_lines_naming_its_worker(self):
        self.run_main()
        for i, s in enumerate(self.schemes, 1):
            tag = rf"\[worker_\d\] \[{i:02d}/24 {s['scheme_code']}\]"
            self.assertEqual(len(self.lines(rf"{tag} SCHEME START")), 1, f"scheme {i}")
            self.assertEqual(len(self.lines(rf"{tag} Attempt 1/3 START")), 1)
            self.assertEqual(len(self.lines(rf"{tag} Attempt 1/3 succeeded in \d+\.\d\ds")), 1)
            self.assertEqual(len(self.lines(rf"{tag} SCHEME DONE status=1 attempts=1 elapsed=\d+\.\d\ds worker=worker_\d")), 1)
            self.assertEqual(len(self.lines(rf"{tag} FILE WRITE {s['id']}\.json \d+ bytes \d+ ms")), 1)
            self.assertEqual(len(self.lines(rf"{tag} STATUS UPDATE scheme={s['scheme_code']} status=1 attempts=1")), 1)

    def test_retries_waits_failures_and_kinds_are_logged(self):
        self.script = {self.code(2): [TIMEOUT, RESET, TIMEOUT], self.code(4): [RESET]}
        self.run_main()
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] Attempt \d/3 START")), 3)
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] Waiting \d+(\.\d)? seconds before retry")), 2)
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] Attempt 1/3 failed after \d+\.\d\ds kind=timeout: ReadTimeout")), 1)
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] Attempt 2/3 failed after \d+\.\d\ds kind=connection-reset")), 1)
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] FINAL STATUS: FAILED after 3 attempts")), 1)
        self.assertEqual(len(self.lines(r"\[02/24 \d+\] SCHEME DONE status=-1 attempts=3")), 1)
        self.assertEqual(len(self.lines(r"\[04/24 \d+\] SCHEME DONE status=1 attempts=2")), 1)
        self.assertRegex(self.log, r"Attempt failures by kind: connection-reset=\d, timeout=2")

    def test_retry_waits_are_logged_as_warnings_and_failures_as_warnings(self):
        self.script = {self.code(2): [TIMEOUT]}
        self.run_main()
        failed = self.lines(r"Attempt 1/3 failed")
        self.assertTrue(failed and all(l.split()[2] == "WARNING" for l in failed))

    def test_status_resets_and_updates_and_final_are_logged(self):
        self.script = {self.code(3): [TIMEOUT] * 3}
        self.run_main()
        self.assertEqual(len(self.lines(r"STATUS RESET execution_id=\S+ schemes=24 all set to status=0 \[crawler_status.json \d+ bytes")), 1)
        self.assertEqual(len(self.lines(r"STATUS UPDATE scheme=")), 24 + 2)
        self.assertEqual(len(self.lines(r"STATUS FINAL overall=WARNING fatal=no")), 1)
        self.assertTrue(self.lines(r"FILE WRITE index.json \d+ bytes \d+ ms \| overall=WARNING successful=23 failed=1 unprocessed=0"))

    def test_the_summary_carries_totals_and_performance_metrics(self):
        self.run_main()
        for pattern in (r"KOSHVANI CRAWLER SUMMARY", r"Total schemes: 24", r"Successful: 24", r"Failed: 0", r"Unprocessed: 0",
                        r"Recovered by retry: 0", r"Fatal crawler error: No", r"Execution status: SUCCESS",
                        r"Wall time: \d+\.\ds \| workers: 4 \| max attempts per scheme: 3",
                        r"Scheme time: min \d+\.\ds \| avg \d+\.\ds \| max \d+\.\ds", r"Slowest schemes: ",
                        r"Worker utilisation: \d+% \| worker_\d: \d+ schemes", r"Time spent waiting between retries: 0\.0s"):
            self.assertRegex(self.log, pattern)

    def test_start_banner_names_the_configuration(self):
        self.run_main()
        self.assertRegex(self.log, r"Config: schemes=24 workers=4 max_attempts=3 retry_delays=5-15s/15-30s request_timeout=45s")
        self.assertRegex(self.log, r"Python \d+\.\d+\.\d+ \| requests \d")

    def test_a_fatal_run_says_so_in_the_log_and_the_summary(self):
        self.script[self.code(7)] = [KeyboardInterrupt("simulated")]
        self.run_main()
        self.assertRegex(self.log, r"ERROR .*Interrupted: simulated")
        self.assertRegex(self.log, r"Execution status: PARTIAL")


class TestHttpLogging(LogCase):
    """The real _fetch / parsing code, against a fake portal."""

    workers = 1

    def setUp(self):
        super().setUp()
        FakePortal.variant, FakePortal.log = "ok", []
        scrape.attempt_scheme = self._saved["attempt_scheme"]
        patcher = mock.patch.object(scrape.requests, "Session", FakePortal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.schemes = self.schemes[:1]
        self.write_config(self.schemes)

    def test_each_request_logs_start_and_end_with_status_size_and_elapsed_time(self):
        self.run_main()
        for what, page_name in (("main page", "KoshvaniStatic.aspx"), ("grant list", "ExpGrant.aspx"),
                                ("scheme list", "ExpHead.aspx"), ("scheme report", "ExpTreas.aspx")):
            self.assertEqual(len(self.lines(rf"HTTP GET {what} \({page_name}\) START")), 1, what)
            self.assertEqual(len(self.lines(rf"HTTP GET {what} \({page_name}\) END status=200 bytes=\d+ elapsed=\d+\.\d\ds")), 1, what)
        self.assertRegex(self.log, r"HTTP requests: 4 \(ok 4, failed 0\) \| downloaded \d+ KB")
        self.assertRegex(self.log, r"Request latency: avg \d+\.\d\ds \| p95 \d+\.\d\ds \| max \d+\.\d\ds")

    def test_parsing_and_validation_are_logged_and_the_result_is_unchanged(self):
        self.assertEqual(self.run_main(), 0)
        self.assertRegex(self.log, r"PARSE report table: columns=9 district_rows=2 fin_year=2026-2027 months=\{'prev_month': 'August', 'current_month': 'September'\}")
        self.assertRegex(self.log, r"VALIDATE ok status=ok rows=2")
        saved = self.scheme_file(1)
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["fin_year"], "2026-2027")
        self.assertEqual((saved["prev_month"], saved["current_month"]), ("August", "September"))
        self.assertEqual(saved["column_headers"], COLUMN_HEADERS)
        self.assertEqual(saved["column_widths"], [11.0] * 9)
        self.assertEqual([r["cells"][0] for r in saved["rows"]], ["G.B.NAGAR", ""], "district only on the first row, exactly as rendered")
        self.assertEqual(saved["totals"], {"progressive_allotment": 1200.0, "expenditure_upto_prev_month": 450.0,
                                           "current_month_expenditure": 110.0, "total_expenditure_upto_month": 560.0,
                                           "pct_expenditure_of_allotment": 46.67})
        self.assertEqual(set(saved), set(ok_result(self.schemes[0])), "the published JSON keeps exactly its old keys")

    def test_each_attempt_starts_from_the_main_page_again(self):
        FakePortal.variant = "timeout"
        self.run_main()
        self.assertEqual(FakePortal.log.count("KoshvaniStatic.aspx"), 3)
        self.assertEqual(FakePortal.log.count("ExpHead.aspx"), 3)

    def test_a_timeout_is_logged_with_its_kind_and_duration(self):
        FakePortal.variant = "timeout"
        self.run_main()
        self.assertEqual(len(self.lines(r"HTTP GET scheme list \(ExpHead.aspx\) FAILED after \d+\.\d\ds kind=timeout: ReadTimeout")), 3)
        self.assertRegex(self.log, r"HTTP requests: 9 \(ok 6, failed 3\)")
        self.assertEqual(self.entry(1)["status"], -1)
        self.assertTrue(self.entry(1)["remark"].startswith("ReadTimeout:"))

    def test_a_connection_reset_is_logged_with_its_kind(self):
        FakePortal.variant = "reset"
        self.run_main()
        self.assertEqual(len(self.lines(r"HTTP GET scheme list \(ExpHead.aspx\) FAILED after \d+\.\d\ds kind=connection-reset: ConnectionError")), 3)

    def test_the_portal_saying_no_record_is_a_logged_success(self):
        FakePortal.variant = "no_record"
        self.run_main()
        self.assertEqual(self.entry(1)["status"], 1)
        self.assertEqual(self.scheme_file(1)["status"], "empty")
        self.assertIn("PARSE portal answered NoRecordFound", self.log)

    def test_a_page_without_the_report_table_is_logged_in_detail_and_retried(self):
        FakePortal.variant = "no_header"
        self.run_main()
        self.assertEqual(len(self.lines(r"PARSE report page has NO table header row: title='ExpTreas.aspx' bytes=\d+ has_Table1=True has_myTable=True")), 3)
        self.assertEqual(len(self.lines(r"kind=validation: ValidationError: Missing expected table")), 3)
        self.assertEqual(self.entry(1), {"status": -1, "attempts": 3, "remark": self.entry(1)["remark"]})
        self.assertTrue(self.entry(1)["remark"].startswith("ValidationError: Missing expected table"))

    def test_no_url_query_string_or_token_reaches_the_log_or_any_json_file(self):
        for variant in ("ok", "timeout", "no_header", "no_record"):
            with self.subTest(variant):
                FakePortal.variant = variant
                self.run_main()
                blob = self.log + "".join(p.read_text(encoding="utf-8") for p in scrape.DATA_DIR.glob("*.json"))
                self.assertNotIn(SECRET_QUERY, blob)
                self.assertNotIn(SECRET_QUERY.split("=")[1][:12], blob)
                self.assertNotRegex(blob, r"\.aspx\?\w", "URLs appear as page names only")


class TestNoSecretsInLogs(LogCase):
    workers = 4

    SECRETS = ["abcd efgh ijkl mnop", "abcdefghijklmnop", "boss@example.org", "team@example.org", "alerts.sender@example.org",
               "tok_live_0123456789abcdef", "SESSIONCOOKIEVALUE123456", "hunter2hunter2", "Zk9x3QvT8rLm2Pq7WnYb4Hc6JdSa1UeVgXi0RoN"]

    def test_secrets_inside_exceptions_never_reach_the_log_the_status_or_the_data(self):
        nasty = [
            RuntimeError("smtp login failed for alerts.sender@example.org with abcd efgh ijkl mnop"),
            requests.exceptions.ConnectionError("HTTPSConnectionPool: Max retries exceeded with url: /Reports/ExpHead.aspx?enc=Zk9x3QvT8rLm2Pq7WnYb4Hc6JdSa1UeVgXi0RoNt5A"),
            ValueError("Cookie: ASP.NET_SessionId=SESSIONCOOKIEVALUE123456; path=/"),
            KeyError("password=hunter2hunter2 token=tok_live_0123456789abcdef sent to boss@example.org"),
        ]
        self.script = {self.code(i + 1): [e, e, e] for i, e in enumerate(nasty)}
        self.run_main()
        blob = self.log + "".join(p.read_text(encoding="utf-8") for p in scrape.DATA_DIR.glob("*.json"))
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob, f"{secret!r} leaked")
        self.assertIn(logsafe.REDACTED, self.log)
        self.assertEqual(self.status()["overall_status"], "WARNING")

    def test_env_values_are_redacted_wherever_they_appear(self):
        for value in ("alerts.sender@example.org", "abcd efgh ijkl mnop", "abcdefghijklmnop", "tok_live_0123456789abcdef", "boss@example.org"):
            self.assertNotIn(value, logsafe.scrub(f"debug dump: [{value}] and {value}!"))

    def test_the_log_formatter_sanitizes_tracebacks_too(self):
        formatter = logsafe.SanitizingFormatter("%(message)s")
        try:
            raise RuntimeError("boom token=tok_live_0123456789abcdef")
        except RuntimeError:
            record = scrape.log.makeRecord("koshvani", 40, __file__, 1, "failed for %s", ("boss@example.org",), sys.exc_info())
        text = formatter.format(record)
        self.assertNotIn("boss@example.org", text)
        self.assertNotIn("tok_live_0123456789abcdef", text)
        self.assertIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
