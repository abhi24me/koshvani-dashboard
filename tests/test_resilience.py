"""Politeness and resilience towards the portal: staggered starts, the portal-wide hold after a
ClearSession bounce, and the cool-down rounds for schemes that still failed after their retries."""
import io
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ScraperCase, scrape  # noqa: E402

TIMEOUT = requests.exceptions.ReadTimeout("Read timed out.")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestStartGate(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for name, value in (("STAGGER_SECONDS", 4), ("STAGGER_JITTER", 0)):
            patcher = mock.patch.object(scrape, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(scrape.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gate = scrape.StartGate()

    def test_defaults(self):
        source = Path(scrape.__file__).read_text(encoding="utf-8")
        for expected in ("STAGGER_SECONDS = 4", "STAGGER_JITTER = 2", "BOUNCE_HOLD_MIN = 60", "BOUNCE_HOLD_MAX = 120",
                         "COOLDOWN_ROUNDS = 2", "COOLDOWN_WAIT = 300"):
            self.assertIn(expected, source)

    def test_simultaneous_requests_get_evenly_spaced_slots(self):
        self.assertEqual([self.gate.reserve() for _ in range(4)], [0, 4, 8, 12])

    def test_a_late_arrival_does_not_wait_for_slots_that_have_passed(self):
        self.gate.reserve()
        self.clock.now += 100
        self.assertEqual(self.gate.reserve(), 0)
        self.assertEqual(self.gate.reserve(), 4)

    def test_slots_are_never_closer_than_the_stagger_and_jitter_only_widens_them(self):
        with mock.patch.object(scrape, "STAGGER_JITTER", 2):
            delays = [self.gate.reserve() for _ in range(200)]
        gaps = [b - a for a, b in zip(delays, delays[1:])]
        self.assertTrue(all(4 <= g <= 6 for g in gaps))
        self.assertGreater(len({round(g, 3) for g in gaps}), 50, "the jitter really varies")

    def test_zero_stagger_means_no_waiting(self):
        with mock.patch.object(scrape, "STAGGER_SECONDS", 0), mock.patch.object(scrape, "STAGGER_JITTER", 0):
            self.assertEqual([self.gate.reserve() for _ in range(5)], [0] * 5)

    def test_a_hold_runs_out_with_time_and_never_shrinks(self):
        self.gate.hold(60)
        self.assertEqual(self.gate.hold_remaining(), 60)
        self.gate.hold(10)                                   # a shorter hold cannot cut the current one short
        self.assertEqual(self.gate.hold_remaining(), 60)
        self.clock.now += 45
        self.assertEqual(self.gate.hold_remaining(), 15)
        self.clock.now += 100
        self.assertEqual(self.gate.hold_remaining(), 0)


class TestStaggeredStarts(ScraperCase):
    workers = 4

    def setUp(self):
        super().setUp()
        scrape._sleep = self._saved["_sleep"]              # real waiting, but with tiny numbers
        scrape.STAGGER_SECONDS, scrape.STAGGER_JITTER = 0.06, 0.02
        self.starts, self.lock = [], threading.Lock()

        def on_attempt(scheme):
            with self.lock:
                self.starts.append(time.monotonic())

        self.on_attempt = on_attempt

    def test_no_two_attempts_start_closer_than_the_stagger(self):
        self.schemes = self.schemes[:10]
        self.write_config(self.schemes)
        self.run_main()
        gaps = [b - a for a, b in zip(sorted(self.starts), sorted(self.starts)[1:])]
        self.assertEqual(len(self.starts), 10)
        self.assertGreaterEqual(min(gaps), 0.03, "two workers hit the portal at (nearly) the same moment")

    def test_retries_are_staggered_too(self):
        self.schemes = self.schemes[:6]
        self.write_config(self.schemes)
        self.script[self.code(2)] = [TIMEOUT, TIMEOUT]
        self.script[self.code(3)] = [TIMEOUT]
        with mock.patch.object(scrape, "retry_delay", lambda n: 0.01):
            self.run_main()
        self.assertEqual(len(self.starts), 6 + 2 + 1)
        gaps = [b - a for a, b in zip(sorted(self.starts), sorted(self.starts)[1:])]
        self.assertGreaterEqual(min(gaps), 0.03)

    def test_the_stagger_is_logged_and_counted(self):
        self.schemes = self.schemes[:5]
        self.write_config(self.schemes)
        self.run_main()
        self.assertRegex(self.log, r"Start stagger: waiting \d+\.\ds so workers do not hit the portal together")
        self.assertRegex(self.log, r"Start stagger / bounce hold: \d+ waits, \d+\.\ds \(summed over workers\) \| portal bounces seen: 0")
        self.assertIn("stagger=0.06+0-0.02s", self.log)

    def test_the_first_attempt_of_a_run_never_waits(self):
        self.schemes = self.schemes[:1]
        self.write_config(self.schemes)
        self.run_main()
        self.assertNotIn("Start stagger: waiting", self.log)


class FakeResponse:
    def __init__(self, url, text="<html></html>", status=200):
        self.url, self.text, self.status_code, self.history = url, text, status, []
        self.content = text.encode("utf-8")


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, timeout=None):
        return self.response


class TestPortalBounce(ScraperCase):
    workers = 4

    def setUp(self):
        super().setUp()
        scrape.STAGGER_SECONDS = scrape.STAGGER_JITTER = 0
        for name, value in (("BOUNCE_HOLD_MIN", 60), ("BOUNCE_HOLD_MAX", 60)):
            patcher = mock.patch.object(scrape, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        scrape.setup_logging()
        scrape._GATE = scrape.StartGate()
        scrape._METRICS = scrape.Metrics()

    def fetch(self, response):
        buf = io.StringIO()
        with redirect_stderr(buf):
            scrape.setup_logging()
            scrape._fetch(FakeSession(response), "https://koshvani.up.nic.in/x.aspx", "grant list")
        return buf.getvalue()

    def test_a_redirect_to_clearsession_holds_everybody(self):
        log = self.fetch(FakeResponse("https://koshvani.up.nic.in/ClearSession.aspx?aspxerrorpath=/KoshReports/ExpGrant.aspx"))
        self.assertIn("PORTAL BOUNCE at grant list: all workers will hold for up to 60s", log)
        self.assertEqual(scrape._GATE.hold_remaining() > 55, True)
        self.assertEqual(scrape._METRICS.bounces, 1)

    def test_a_tiny_clearsession_page_served_under_a_normal_url_is_a_bounce_too(self):
        self.fetch(FakeResponse("https://koshvani.up.nic.in/Reports/ExpTreas.aspx", "<script>location='ClearSession.aspx'</script>"))
        self.assertGreater(scrape._GATE.hold_remaining(), 55)

    def test_a_normal_page_never_triggers_a_hold(self):
        self.fetch(FakeResponse("https://koshvani.up.nic.in/Reports/ExpGrant.aspx", "x" * 5000))
        self.fetch(FakeResponse("https://koshvani.up.nic.in/Reports/ExpTreas.aspx", "ClearSession is mentioned in a big page " + "x" * 5000))
        self.assertEqual(scrape._GATE.hold_remaining(), 0)
        self.assertEqual(scrape._METRICS.bounces, 0)

    def test_every_worker_waits_out_the_hold_before_its_next_attempt(self):
        seen = {"bounced": False}

        def on_attempt(scheme):
            if scheme["scheme_code"] == self.code(1) and not seen["bounced"]:
                seen["bounced"] = True
                scrape._note_portal_bounce("grant list")

        self.on_attempt = on_attempt
        self.run_main()
        holds = [s for s in self.sleeps if 55 <= s <= 60]
        self.assertGreaterEqual(len(holds), 10, "the workers' later attempts all paused")
        self.assertRegex(self.log, r"Portal hold: the portal is bouncing sessions, waiting \d+s with the other workers")
        self.assertRegex(self.log, r"portal bounces seen: 1")
        self.assertEqual(self.status()["overall_status"], "SUCCESS")

    def test_a_hold_does_not_change_the_outcome_only_the_timing(self):
        scrape._note_portal_bounce("test")
        self.run_main()
        self.assertEqual(self.status()["overall_status"], "SUCCESS")
        self.assertEqual({e["attempts"] for e in self.status()["schemes"].values()}, {1})


class TestCoolDownRounds(ScraperCase):
    def setUp(self):
        super().setUp()
        scrape.COOLDOWN_ROUNDS, scrape.COOLDOWN_WAIT = 2, 300

    def waits(self):
        return [s for s in self.sleeps if s == 300]

    def test_a_scheme_that_failed_its_retries_gets_a_second_chance_after_the_pause(self):
        self.script[self.code(5)] = [TIMEOUT] * 3                 # the whole first turn fails, round 2 succeeds
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.entry(5), {"status": 1, "attempts": 4, "remark": "Recovered in cool-down round 1"})
        self.assertEqual(self.waits(), [300], "one pause, before the one round that was needed")
        self.assertEqual(self.calls.count(self.code(5)), 4)
        self.assertEqual(self.status()["overall_status"], "SUCCESS")
        self.assertEqual(self.scheme_file(5)["status"], "ok", "the error stub was replaced by real data")
        self.assertRegex(self.log, r"COOL-DOWN round 1/2: 1 scheme\(s\) still failed; waiting 300s")
        self.assertRegex(self.log, r"\[05/24 \d+\] SCHEME START \[cool-down round 1/2\]")

    def test_a_second_round_is_used_when_the_first_did_not_help(self):
        self.script[self.code(5)] = [TIMEOUT] * 6
        self.run_main()
        self.assertEqual(self.entry(5), {"status": 1, "attempts": 7, "remark": "Recovered in cool-down round 2"})
        self.assertEqual(self.waits(), [300, 300])

    def test_a_scheme_that_never_recovers_ends_failed_after_every_round(self):
        self.script[self.code(5)] = [TIMEOUT] * 20
        self.assertEqual(self.run_main(), 0)                        # WARNING is still a completed run
        self.assertEqual(self.entry(5)["status"], -1)
        self.assertEqual(self.entry(5)["attempts"], 9)
        self.assertTrue(self.entry(5)["remark"].startswith("ReadTimeout:"))
        self.assertEqual(self.waits(), [300, 300])
        self.assertEqual(self.calls.count(self.code(5)), 9)
        self.assertEqual(self.status()["overall_status"], "WARNING")
        self.assertEqual(self.scheme_file(5)["status"], "error")

    def test_only_the_failed_schemes_are_retried_and_a_clean_run_never_pauses(self):
        self.run_main()
        self.assertEqual(self.waits(), [])
        self.assertNotIn("COOL-DOWN", self.log)
        self.script = {self.code(3): [TIMEOUT] * 3, self.code(9): [TIMEOUT] * 3}
        self.calls.clear()
        self.run_main()
        for i in range(1, 25):
            self.assertEqual(self.calls.count(self.code(i)), 4 if i in (3, 9) else 1, f"scheme {i}")

    def test_the_cool_down_can_be_switched_off(self):
        scrape.COOLDOWN_ROUNDS = 0
        self.script[self.code(5)] = [TIMEOUT] * 3
        self.run_main()
        self.assertEqual(self.entry(5), {"status": -1, "attempts": 3, "remark": self.entry(5)["remark"]})
        self.assertEqual(self.waits(), [])

    def test_no_cool_down_after_a_fatal_error(self):
        self.script[self.code(2)] = [TIMEOUT] * 3
        self.script[self.code(6)] = [KeyboardInterrupt("simulated")]
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        self.assertEqual(self.waits(), [])
        self.assertEqual(self.status()["overall_status"], "PARTIAL")
        self.assertEqual(self.entry(2)["status"], -1)

    def test_a_stop_request_during_the_pause_skips_the_round(self):
        self.script[self.code(5)] = [TIMEOUT] * 3
        scrape._sleep = lambda seconds: scrape._STOP.set() if seconds == 300 else None
        self.run_main()
        self.assertEqual(self.calls.count(self.code(5)), 3)
        self.assertEqual(self.entry(5)["status"], -1)

    def test_unprocessed_schemes_are_not_touched_by_the_cool_down(self):
        self.script[self.code(2)] = [TIMEOUT] * 3
        self.script[self.code(6)] = [KeyboardInterrupt("simulated")]
        self.run_main()
        for i in range(7, 25):
            self.assertEqual(self.entry(i), {"status": 0, "attempts": 0, "remark": ""})

    def test_the_status_index_and_counts_stay_consistent(self):
        self.script[self.code(4)] = [TIMEOUT] * 3
        self.script[self.code(8)] = [TIMEOUT] * 20
        self.run_main()
        idx = self.index()
        self.assertEqual((idx["successful_scheme_count"], idx["failed_scheme_count"], idx["unprocessed_scheme_count"]), (23, 1, 0))
        self.assertEqual(idx["overall_status"], "WARNING")
        self.assertEqual(self.status()["schemes"][self.code(4)]["attempts"], 4)
        self.assertRegex(self.log, r"Recovered by retry: 1")

    def test_the_config_line_documents_the_settings(self):
        self.run_main()
        self.assertIn("cooldown_rounds=2x300s", self.log)
        self.assertIn("bounce_hold=60-120s", self.log)


class TestCoolDownWithWorkers(ScraperCase):
    workers = 4

    def test_concurrent_run_recovers_failed_schemes_in_the_cool_down_round(self):
        scrape.COOLDOWN_ROUNDS, scrape.COOLDOWN_WAIT = 2, 300
        self.script = {self.code(i): [TIMEOUT] * 3 for i in (2, 7, 12, 19)}
        self.assertEqual(self.run_main(), 0)
        st = self.status()
        self.assertEqual(st["overall_status"], "SUCCESS")
        for i in (2, 7, 12, 19):
            self.assertEqual(self.entry(i), {"status": 1, "attempts": 4, "remark": "Recovered in cool-down round 1"})
        self.assertEqual([s for s in self.sleeps if s == 300], [300])
        self.assertEqual(self.index()["successful_scheme_count"], 24)
        self.assertTrue(all(self.scheme_file(i)["status"] == "ok" for i in range(1, 25)))


if __name__ == "__main__":
    unittest.main()
