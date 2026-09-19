"""Per-scheme retry, persistence, status tracking and crash safety of scraper/scrape.py."""
import io
import itertools
import json
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import LEGACY_INDEX_KEYS, ScraperCase, make_schemes, ok_result, scrape  # noqa: E402

def ticking_clock():
    """A fake scrape.utc_now(): every call is one second later than the last."""
    ticks = itertools.count()
    return lambda: (datetime(2026, 9, 19, 14, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")


TIMEOUT = requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='koshvani.up.nic.in', port=443): Read timed out. (read timeout=45)")


class TestRetries(ScraperCase):
    def test_01_all_24_succeed_on_first_attempt(self):
        self.assertEqual(self.run_main(), 0)
        st = self.status()
        self.assertEqual(st["overall_status"], "SUCCESS")
        self.assertEqual(len(st["schemes"]), 24)
        for i in range(1, 25):
            self.assertEqual(self.entry(i), {"status": 1, "attempts": 1, "remark": ""})
        self.assertEqual(len(self.calls), 24)
        self.assertEqual(self.sleeps, [], "no sleeping after a success")
        idx = self.index()
        self.assertEqual((idx["successful_scheme_count"], idx["failed_scheme_count"], idx["unprocessed_scheme_count"]), (24, 0, 0))
        self.assertEqual(idx["overall_status"], "SUCCESS")

    def test_02_fails_once_recovers_on_attempt_2(self):
        self.script[self.code(7)] = [TIMEOUT]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.entry(7), {"status": 1, "attempts": 2, "remark": "Recovered on retry"})
        self.assertEqual(self.status()["overall_status"], "SUCCESS")
        self.assertEqual(self.calls.count(self.code(7)), 2)

    def test_03_fails_twice_recovers_on_attempt_3(self):
        self.script[self.code(4)] = [TIMEOUT, requests.exceptions.ConnectionError("boom")]
        self.run_main()
        self.assertEqual(self.entry(4), {"status": 1, "attempts": 3, "remark": "Recovered on retry"})
        self.assertEqual(self.status()["overall_status"], "SUCCESS")
        self.assertEqual(self.calls.count(self.code(4)), 3)

    def test_04_fails_all_three_attempts(self):
        errors = [TIMEOUT, requests.exceptions.SSLError("first ssl"), requests.exceptions.ConnectionError("LATEST failure")]
        self.script[self.code(9)] = errors
        self.assertEqual(self.run_main(), 0, "a failed scheme is a WARNING, never a crawler failure")
        e = self.entry(9)
        self.assertEqual((e["status"], e["attempts"]), (-1, 3))
        self.assertTrue(e["remark"].startswith("ConnectionError: LATEST failure"), e["remark"])
        self.assertEqual(self.status()["overall_status"], "WARNING")
        self.assertEqual(self.calls.count(self.code(9)), 3, "never a 4th attempt")
        self.assertEqual(self.scheme_file(9)["status"], "error")          # dashboard shows its 'Fetch error' state
        self.assertIn("LATEST failure", self.scheme_file(9)["message"])
        self.assertEqual(self.index()["failed_scheme_count"], 1)

    def test_05_multiple_failures_are_independent_and_do_not_stop_the_rest(self):
        e = requests.exceptions.ConnectionError("down")
        self.script = {self.code(2): [e, e, e], self.code(5): [e], self.code(9): [e, e, e]}
        self.run_main()
        expected = {2: (-1, 3), 5: (1, 2), 9: (-1, 3)}
        for i in range(1, 25):
            got = (self.entry(i)["status"], self.entry(i)["attempts"])
            self.assertEqual(got, expected.get(i, (1, 1)), f"scheme {i}")
        self.assertEqual(self.status()["overall_status"], "WARNING")
        self.assertEqual(len(self.calls), 24 + 2 + 1 + 2)

    def test_06_successful_scheme_is_never_retried(self):
        self.script[self.code(3)] = [TIMEOUT]
        self.run_main()
        for i in range(1, 25):
            if i != 3:
                self.assertEqual(self.calls.count(self.code(i)), 1, f"scheme {i}")

    def test_06b_retries_happen_inside_the_scheme_turn_not_in_a_later_phase(self):
        e = requests.exceptions.ConnectionError("x")
        self.script = {self.code(2): [e, e], self.code(6): [e], self.code(24): [e]}
        self.run_main()
        collapsed = [c for n, c in enumerate(self.calls) if n == 0 or c != self.calls[n - 1]]
        self.assertEqual(collapsed, [s["scheme_code"] for s in self.schemes],
                         "each scheme's attempts must be consecutive - no separate retry pass afterwards")

    def test_07_delays_are_randomized_within_the_configured_ranges(self):
        for retry, low, high in ((1, scrape.RETRY_1_DELAY_MIN, scrape.RETRY_1_DELAY_MAX), (2, scrape.RETRY_2_DELAY_MIN, scrape.RETRY_2_DELAY_MAX)):
            samples = [scrape.retry_delay(retry) for _ in range(400)]
            self.assertTrue(all(low <= s <= high for s in samples), f"retry {retry} out of range")
            self.assertGreater(len(set(samples)), 20, "delays must vary, not be a fixed sleep")
        self.assertEqual((scrape.RETRY_1_DELAY_MIN, scrape.RETRY_1_DELAY_MAX, scrape.RETRY_2_DELAY_MIN, scrape.RETRY_2_DELAY_MAX), (5, 15, 15, 30))
        self.assertEqual(scrape.MAX_ATTEMPTS, 3)
        with mock.patch.object(scrape.random, "uniform", return_value=6.5) as uni:
            scrape.retry_delay(1)
            scrape.retry_delay(2)
        self.assertEqual([c.args for c in uni.call_args_list], [(5, 15), (15, 30)])

    def test_07b_run_sleeps_once_per_retry_in_the_right_range(self):
        e = requests.exceptions.ConnectionError("x")
        self.script[self.code(8)] = [e, e]              # fails twice -> two sleeps, then succeeds
        self.run_main()
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(scrape.RETRY_1_DELAY_MIN <= self.sleeps[0] <= scrape.RETRY_1_DELAY_MAX)
        self.assertTrue(scrape.RETRY_2_DELAY_MIN <= self.sleeps[1] <= scrape.RETRY_2_DELAY_MAX)

    def test_error_in_any_form_is_a_scheme_level_failure(self):
        self.script[self.code(3)] = [AttributeError("parser bug")] * 3      # not a requests/RuntimeError type
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.entry(3)["status"], -1)
        self.assertTrue(self.entry(3)["remark"].startswith("AttributeError: parser bug"))
        self.assertEqual(self.entry(24)["status"], 1, "the other schemes carried on")


class TestStatusFile(ScraperCase):
    def test_08_status_saved_to_disk_after_every_state_change(self):
        self.script[self.code(5)] = [TIMEOUT, requests.exceptions.ConnectionError("second")]
        snaps, orig = [], scrape.CrawlStatus._save

        def spy(crawl, *args):
            orig(crawl, *args)
            snaps.append(self.status()["schemes"][self.code(5)])      # read back from DISK

        with mock.patch.object(scrape.CrawlStatus, "_save", spy):
            self.run_main()
        seq = [s for n, s in enumerate(snaps) if n == 0 or s != snaps[n - 1]]
        self.assertEqual([(s["status"], s["attempts"]) for s in seq], [(0, 0), (-1, 1), (-1, 2), (1, 3)])
        self.assertTrue(seq[1]["remark"].startswith("ReadTimeout:") and seq[2]["remark"].startswith("ConnectionError: second"))
        self.assertEqual(seq[3]["remark"], "Recovered on retry")

    def test_structure_and_keys(self):
        self.run_main()
        st = self.status()
        self.assertEqual(set(st), {"execution_id", "started_at", "updated_at", "finished_at", "overall_status", "fatal_error", "schemes"})
        self.assertEqual(list(st["schemes"]), [s["scheme_code"] for s in self.schemes], "keyed by numeric scheme code, in config order")
        self.assertTrue(all(c.isdigit() for c in st["schemes"]))
        for entry in st["schemes"].values():
            self.assertEqual(set(entry), {"status", "attempts", "remark"})

    def test_keys_come_from_schemes_json_not_a_hardcoded_list(self):
        self.write_config(make_schemes(5))
        self.schemes = make_schemes(5)
        self.run_main()
        self.assertEqual(list(self.status()["schemes"]), [s["scheme_code"] for s in make_schemes(5)])

    def test_no_links_cookies_or_secrets_are_stored(self):
        secret = "ENCRYPTEDTOKEN9f8e7d"
        self.script[self.code(2)] = [
            requests.exceptions.HTTPError(f"500 Server Error: Internal Server Error for url: https://koshvani.up.nic.in/Reports/ExpTreas.aspx?enc={secret}"),
            requests.exceptions.ConnectionError(f"HTTPSConnectionPool(host='koshvani.up.nic.in', port=443): Max retries exceeded with url: /Reports/ExpHead.aspx?enc={secret} (Caused by X)"),
            RuntimeError(f"Expected ExpTreas.aspx after selecting scheme, got https://koshvani.up.nic.in/Reports/NoRecordFound.htm?enc={secret}"),
        ]
        self.run_main()
        text = (scrape.DATA_DIR / "crawler_status.json").read_text(encoding="utf-8")
        published = (scrape.DATA_DIR / f"{self.schemes[1]['id']}.json").read_text(encoding="utf-8")
        for blob in (text, published):
            self.assertNotIn(secret, blob)
            self.assertNotIn("https://", blob)
            self.assertNotIn("cookie", blob.lower())
        for forbidden in ("link", "link1", "url", "session", "authorization"):
            self.assertNotIn(f'"{forbidden}"', text)
        self.assertIn("ExpTreas.aspx", self.entry(2)["remark"], "the page name is kept for diagnosis")

    def test_remark_is_bounded(self):
        self.script[self.code(1)] = [RuntimeError("x" * 5000)] * 3
        self.run_main()
        self.assertLessEqual(len(self.entry(1)["remark"]), scrape.MAX_REMARK_LENGTH)

    def test_19_only_one_status_file_across_many_executions(self):
        scrape.utc_now = ticking_clock()
        for _ in range(3):
            self.run_main()
        names = sorted(p.name for p in scrape.DATA_DIR.iterdir())
        self.assertEqual([n for n in names if "status" in n.lower()], ["crawler_status.json"])
        self.assertFalse([n for n in names if n.endswith(".tmp")])
        self.assertEqual(len(names), 24 + 2, "24 scheme files + index.json + crawler_status.json, nothing else")

    def test_20_new_execution_resets_every_status_before_starting(self):
        e = requests.exceptions.ConnectionError("down")
        self.script[self.code(3)] = [e, e, e]
        self.run_main()
        first_files = {p.name: p.read_bytes() for p in scrape.DATA_DIR.glob("agri-*.json")}
        seen = {}

        def at_first_attempt(scheme):
            if not seen:
                seen.update(self.status()["schemes"])       # state on disk just before scheme 1 is processed

        self.script, self.calls, self.on_attempt = {}, [], at_first_attempt
        self.run_main()
        for i in range(1, 25):        # captured just before scheme 1 was attempted in the second run
            self.assertEqual(seen[self.code(i)], {"status": 0, "attempts": 0, "remark": ""}, f"scheme {i} not reset")
        self.assertEqual(self.status()["overall_status"], "SUCCESS")
        self.assertEqual(set(first_files), {p.name for p in scrape.DATA_DIR.glob("agri-*.json")}, "scheme data is never deleted by a reset")

    def test_21_previous_execution_values_do_not_carry_over(self):
        e = requests.exceptions.ConnectionError("down")
        self.script[self.code(3)] = [e, e, e]
        self.run_main()
        self.assertEqual(self.entry(3)["status"], -1)
        self.script, self.calls = {}, []
        self.run_main()
        self.assertEqual(self.entry(3), {"status": 1, "attempts": 1, "remark": ""})
        self.assertEqual(self.status()["overall_status"], "SUCCESS")

    def test_22_execution_id_and_timestamps_change_every_execution(self):
        scrape.utc_now = ticking_clock()
        self.run_main()
        one = self.status()
        self.run_main()
        two = self.status()
        self.assertNotEqual(one["execution_id"], two["execution_id"])
        self.assertEqual(two["execution_id"], two["started_at"])
        self.assertLess(one["started_at"], two["started_at"])
        for st in (one, two):
            self.assertLessEqual(st["started_at"], st["updated_at"])
            self.assertLessEqual(st["updated_at"], st["finished_at"])
            self.assertIsNotNone(st["finished_at"])


class TestPersistenceAndCrashes(ScraperCase):
    def test_09_each_scheme_file_is_on_disk_before_the_next_scheme_starts(self):
        violations, first_seen = [], set()

        def check_previous(scheme):
            i = [s["id"] for s in self.schemes].index(scheme["id"])
            if i == 0 or scheme["id"] in first_seen:
                return
            first_seen.add(scheme["id"])
            prev = self.schemes[i - 1]
            path = scrape.DATA_DIR / f"{prev['id']}.json"
            if not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("status") != "ok":
                violations.append(prev["id"])
            if self.status()["schemes"][prev["scheme_code"]]["status"] != 1:
                violations.append("status:" + prev["id"])

        self.on_attempt = check_previous
        self.run_main()
        self.assertEqual(violations, [])
        self.assertEqual(len(first_seen), 23)

    def test_10_crash_after_10_schemes_keeps_their_data(self):
        self.script[self.code(11)] = [KeyboardInterrupt("simulated crash")]
        rc = self.run_main()
        self.assertEqual(rc, scrape.EXIT_INCOMPLETE)
        st = self.status()
        self.assertEqual(st["overall_status"], "PARTIAL")
        self.assertIn("Interrupted", st["fatal_error"])
        for i in range(1, 11):
            self.assertEqual(self.entry(i)["status"], 1, f"scheme {i}")
            self.assertEqual(self.scheme_file(i)["status"], "ok", "no rollback of successful data")
        for i in range(11, 25):
            self.assertEqual(self.entry(i), {"status": 0, "attempts": 0, "remark": ""}, f"scheme {i} must stay 0, not -1")
        idx = self.index()
        self.assertEqual((idx["successful_scheme_count"], idx["failed_scheme_count"], idx["unprocessed_scheme_count"]), (10, 0, 14))
        self.assertEqual(idx["overall_status"], "PARTIAL")
        self.assertNotEqual(idx["successful_scheme_count"], idx["expected_scheme_count"], "must not claim 24/24")

    def test_23_fatal_error_leaves_unattempted_schemes_at_zero(self):
        real_save = scrape.save_result

        def failing_save(scheme, result):                       # e.g. disk full: a persistence failure is FATAL, not a scheme failure
            if scheme["id"] == self.schemes[11]["id"]:
                raise OSError(28, "No space left on device")
            return real_save(scheme, result)

        scrape.save_result = failing_save
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        st = self.status()
        self.assertEqual(st["overall_status"], "PARTIAL")
        self.assertIn("Fatal crawler error: OSError", st["fatal_error"])
        self.assertTrue(all(self.entry(i)["status"] == 1 for i in range(1, 12)))
        self.assertEqual(self.entry(12), {"status": 0, "attempts": 0, "remark": ""}, "scraped but never persisted: not recorded as a success")
        self.assertTrue(all(self.entry(i)["status"] == 0 for i in range(13, 25)))
        self.assertEqual(self.calls.count(self.code(12)), 1, "a disk error must not be retried as if it were a portal error")

    def test_process_killed_without_finalizing_leaves_a_consistent_disk_state(self):
        scrape.DATA_DIR.mkdir(parents=True)
        crawl = scrape.CrawlStatus(self.schemes)
        crawl.start()
        scrape.write_index(self.schemes, crawl)
        with redirect_stderr(io.StringIO()):
            for i, s in enumerate(self.schemes[:10], 1):
                scrape.process_scheme(i, 24, s, crawl)
                scrape.write_index(self.schemes, crawl)
        # ...SIGKILL here: finalize() never runs
        st = self.status()
        self.assertEqual(st["overall_status"], "RUNNING")
        self.assertIsNone(st["finished_at"])
        self.assertEqual(sum(1 for e in st["schemes"].values() if e["status"] == 1), 10)
        self.assertEqual(sum(1 for e in st["schemes"].values() if e["status"] == 0), 14)
        idx = self.index()
        self.assertEqual((idx["overall_status"], idx["successful_scheme_count"], idx["unprocessed_scheme_count"]), ("RUNNING", 10, 14))
        for i in range(1, 11):
            self.assertEqual(self.scheme_file(i)["status"], "ok")

    def test_fatal_startup_error_is_recorded_not_a_crash(self):
        scrape.CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        st = self.status()
        self.assertEqual((st["overall_status"], st["schemes"]), ("ERROR", {}))
        self.assertTrue(st["fatal_error"].startswith("Fatal crawler error:"))

    def test_duplicate_scheme_codes_are_refused(self):
        dup = make_schemes(3)
        dup[2]["scheme_code"] = dup[0]["scheme_code"]
        self.write_config(dup)
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        self.assertIn("Duplicate scheme_code", self.status()["fatal_error"])

    def test_nothing_attempted_is_an_error_not_a_partial(self):
        self.script[self.code(1)] = [KeyboardInterrupt("killed at the very start")]
        self.run_main()
        self.assertEqual(self.status()["overall_status"], "ERROR")
        self.assertTrue(all(e["status"] == 0 for e in self.status()["schemes"].values()))

    def test_index_for_never_scraped_scheme_is_a_placeholder(self):
        self.script[self.code(1)] = [KeyboardInterrupt("x")]
        self.run_main()
        first = self.index()["schemes"][0]
        self.assertEqual((first["status"], first["crawl_status"], first["generated_at"]), ("pending", 0, None))


class TestAtomicWrites(ScraperCase):
    def test_16_write_leaves_no_tmp_file_and_valid_json(self):
        target = self.tmp / "a.json"
        scrape.write_json_atomic(target, {"v": 1, "text": "कृषि"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": 1, "text": "कृषि"})
        self.assertFalse((self.tmp / "a.json.tmp").exists())

    def test_16_failed_replace_keeps_the_previous_file_intact(self):
        target = self.tmp / "a.json"
        scrape.write_json_atomic(target, {"v": 1})
        with mock.patch.object(scrape.os, "replace", side_effect=OSError("killed mid-replace")):
            with self.assertRaises(OSError):
                scrape.write_json_atomic(target, {"v": 2})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": 1})

    def test_16_unserializable_data_never_touches_the_target(self):
        target = self.tmp / "a.json"
        scrape.write_json_atomic(target, {"v": 1})
        with self.assertRaises(TypeError):
            scrape.write_json_atomic(target, {"v": object()})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"v": 1})
        self.assertFalse((self.tmp / "a.json.tmp").exists())

    def test_16_half_written_tmp_from_a_killed_run_is_cleaned_at_startup(self):
        scrape.DATA_DIR.mkdir(parents=True)
        (scrape.DATA_DIR / "agri-x.json.tmp").write_text('{"half": ', encoding="utf-8")
        (scrape.DATA_DIR / "crawler_status.json.tmp").write_text("{", encoding="utf-8")
        self.run_main()
        self.assertEqual(list(scrape.DATA_DIR.glob("*.tmp")), [])

    def test_16_every_output_file_is_written_atomically(self):
        used = []
        real = scrape.write_json_atomic
        with mock.patch.object(scrape, "write_json_atomic", side_effect=lambda p, d: (used.append(Path(p).name), real(p, d))[1]):
            self.run_main()
        self.assertIn("crawler_status.json", used)
        self.assertIn("index.json", used)
        self.assertTrue(all(f"{s['id']}.json" in used for s in self.schemes))


class TestValidationAndErrors(ScraperCase):
    """These use the real attempt_scheme() with only the portal round-trip faked."""

    def setUp(self):
        super().setUp()
        scrape.attempt_scheme = self._saved["attempt_scheme"]
        self.portal = []
        self._patch = mock.patch.object(scrape, "_scrape_scheme_once", side_effect=lambda session, scheme: self._portal(scheme))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _portal(self, scheme):
        outcome = self.portal.pop(0) if self.portal else ok_result(scheme)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(scheme) if callable(outcome) else outcome

    def test_result_without_the_table_is_a_validation_failure_and_retried(self):
        def no_headers(scheme):
            r = ok_result(scheme)
            r["column_headers"] = []
            return r
        self.portal = [no_headers, no_headers, no_headers]
        self.write_config(make_schemes(1))
        self.schemes = make_schemes(1)
        self.run_main()
        self.assertEqual((self.entry(1)["status"], self.entry(1)["attempts"]), (-1, 3))
        self.assertTrue(self.entry(1)["remark"].startswith("ValidationError: Missing expected table"), self.entry(1)["remark"])

    def test_real_exception_names_are_kept_in_the_remark(self):
        self.write_config(make_schemes(2))
        self.schemes = make_schemes(2)
        self.portal = [TIMEOUT, TIMEOUT, TIMEOUT]
        self.run_main()
        self.assertTrue(self.entry(1)["remark"].startswith("ReadTimeout: HTTPSConnectionPool"), self.entry(1)["remark"])
        self.assertEqual(self.entry(2)["status"], 1)

    def test_legitimate_empty_portal_answers_are_success_with_an_informative_remark(self):
        def empty(scheme):
            return {**scrape.base_meta(scheme, "2026-2027"), "status": "empty", "message": "No expenditure recorded for this scheme in the current period.",
                    "column_headers": [], "column_widths": None, "rows": [], "totals": {}}
        self.write_config(make_schemes(1))
        self.schemes = make_schemes(1)
        self.portal = [empty]
        self.run_main()
        self.assertEqual((self.entry(1)["status"], self.entry(1)["attempts"]), (1, 1))
        self.assertIn("No expenditure recorded", self.entry(1)["remark"])
        self.assertEqual(self.scheme_file(1)["status"], "empty")

    def test_describe_error_names_the_class_and_hides_links(self):
        text = scrape.describe_error(RuntimeError("got https://koshvani.up.nic.in/Reports/ExpHead.aspx?enc=SECRET bad"))
        self.assertEqual(text, "RuntimeError: got ExpHead.aspx bad")
        self.assertTrue(scrape.describe_error(scrape.MissingLinkError("Could not find x")).startswith("MissingLinkError: "))

    def test_validate_result_rejects_malformed_results(self):
        scheme = make_schemes(1)[0]
        good = ok_result(scheme)
        self.assertIs(scrape.validate_result(good, scheme), good)
        for label, mutate in (("wrong status", lambda r: r.update(status="error")),
                              ("wrong scheme", lambda r: r.update(scheme_code="1")),
                              ("no rows", lambda r: r.update(rows=[])),
                              ("short row", lambda r: r.update(rows=[{"cells": ["a"]}])),
                              ("no totals", lambda r: r.update(totals={}))):
            bad = ok_result(scheme)
            mutate(bad)
            with self.assertRaises(scrape.ValidationError, msg=label):
                scrape.validate_result(bad, scheme)


class TestIndexCompatibility(ScraperCase):
    def test_17_index_keeps_every_field_the_dashboard_reads(self):
        self.run_main()
        idx = self.index()
        self.assertIsInstance(idx["schemes"], list)
        self.assertIn("generated_at", idx)
        self.assertEqual(len(idx["schemes"]), 24)
        for entry in idx["schemes"]:
            self.assertTrue(LEGACY_INDEX_KEYS <= set(entry), LEGACY_INDEX_KEYS - set(entry))
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["crawl_status"], 1)
            self.assertEqual(entry["progressive_allotment"], 1000.0)
        for key in ("execution_id", "overall_status", "expected_scheme_count", "successful_scheme_count", "failed_scheme_count", "unprocessed_scheme_count"):
            self.assertIn(key, idx)

    def test_17_scheme_file_format_is_unchanged(self):
        self.run_main()
        keys = set(self.scheme_file(1))
        self.assertEqual(keys, {"id", "name", "grant_text", "scheme_code", "district", "fin_year", "generated_at", "status", "message",
                                "prev_month", "current_month", "column_headers", "column_widths", "rows", "totals"})

    def test_committed_index_json_still_satisfies_the_legacy_contract(self):
        real = json.loads((Path(__file__).resolve().parent.parent / "docs" / "data" / "index.json").read_text(encoding="utf-8"))
        for entry in real["schemes"]:
            self.assertTrue(LEGACY_INDEX_KEYS <= set(entry))

    def test_index_never_claims_full_success_when_something_failed(self):
        e = requests.exceptions.ConnectionError("down")
        self.script[self.code(2)] = [e, e, e]
        self.run_main()
        idx = self.index()
        self.assertEqual((idx["overall_status"], idx["successful_scheme_count"], idx["failed_scheme_count"]), ("WARNING", 23, 1))


if __name__ == "__main__":
    unittest.main()
