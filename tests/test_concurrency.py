"""The thread-pool architecture: real parallelism (bounded by MAX_WORKERS), isolation
between schemes, and files that are never half-written however the workers interleave."""
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ScraperCase, ok_result, scrape  # noqa: E402

TIMEOUT = requests.exceptions.ReadTimeout("Read timed out.")
RESET = requests.exceptions.ConnectionError("Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))


class TestPoolShape(ScraperCase):
    workers = 4

    def test_default_is_four_workers(self):
        self.assertEqual(self._saved["MAX_WORKERS"], 4)

    def test_four_schemes_really_run_at_the_same_time(self):
        barrier = threading.Barrier(4, timeout=10)      # only opens when four attempts are in flight together
        first_four = {s["scheme_code"] for s in self.schemes[:4]}

        def on_attempt(scheme):
            if scheme["scheme_code"] in first_four:
                barrier.wait()

        self.on_attempt = on_attempt
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(barrier.broken)
        self.assertEqual(self.status()["overall_status"], "SUCCESS")

    def test_never_more_than_max_workers_at_once(self):
        for limit in (1, 3, 4):
            with self.subTest(workers=limit):
                scrape.MAX_WORKERS = limit
                lock, state = threading.Lock(), {"now": 0, "peak": 0}

                def on_attempt(scheme):
                    with lock:
                        state["now"] += 1
                        state["peak"] = max(state["peak"], state["now"])
                    time.sleep(0.02)
                    with lock:
                        state["now"] -= 1

                self.on_attempt, self.calls = on_attempt, []
                self.run_main()
                self.assertEqual(state["peak"], limit)

    def test_pool_is_never_larger_than_the_number_of_schemes(self):
        self.schemes = self.schemes[:2]
        self.write_config(self.schemes)
        self.run_main()
        self.assertIn("workers=2", self.log)

    def test_worker_threads_are_named_in_the_log(self):
        self.run_main()
        names = set(re.findall(r"\[(worker_\d+)\]", self.log))
        self.assertEqual(len(names), 4)


class TestIsolation(ScraperCase):
    workers = 4

    def test_mixed_outcomes_give_the_right_final_state(self):
        # 2 fails 3x, 5 recovers on attempt 2, 9 recovers on attempt 3, 14 times out then resets then works
        self.script = {
            self.code(2): [TIMEOUT, TIMEOUT, TIMEOUT],
            self.code(5): [RESET],
            self.code(9): [TIMEOUT, RESET],
            self.code(14): [ValueError("bad page")] * 3,
        }
        rc = self.run_main()
        self.assertEqual(rc, 0)                                      # WARNING is a completed run
        st = self.status()
        self.assertEqual(st["overall_status"], "WARNING")
        for i, (status, attempts) in {2: (-1, 3), 5: (1, 2), 9: (1, 3), 14: (-1, 3)}.items():
            self.assertEqual((self.entry(i)["status"], self.entry(i)["attempts"]), (status, attempts), f"scheme {i}")
        for i in set(range(1, 25)) - {2, 5, 9, 14}:
            self.assertEqual(self.entry(i), {"status": 1, "attempts": 1, "remark": ""}, f"scheme {i}")
        self.assertEqual(self.scheme_file(2)["status"], "error")
        self.assertEqual(self.scheme_file(5)["status"], "ok")
        idx = self.index()
        self.assertEqual((idx["successful_scheme_count"], idx["failed_scheme_count"], idx["unprocessed_scheme_count"]), (22, 2, 0))
        self.assertEqual([e["id"] for e in idx["schemes"]], [s["id"] for s in self.schemes], "index keeps schemes.json order")
        self.assertEqual(sum(self.calls.count(self.code(i)) for i in range(1, 25)), 31)

    def test_a_failing_scheme_never_disturbs_its_neighbours(self):
        self.script = {self.code(i): [TIMEOUT] * 3 for i in (3, 4, 5, 6)}       # four workers all failing at once
        self.run_main()
        self.assertEqual(self.status()["overall_status"], "WARNING")
        for i in range(1, 25):
            self.assertEqual(self.entry(i)["status"], -1 if i in (3, 4, 5, 6) else 1)
            self.assertEqual(self.scheme_file(i)["id"], self.schemes[i - 1]["id"], "each file holds its own scheme's data")

    def test_retry_waits_only_hold_up_the_worker_that_needs_them(self):
        self.script[self.code(1)] = [TIMEOUT, TIMEOUT]
        others_done_during_wait = []

        def waiting(seconds):                                        # scheme 1 "sleeps" until the other 23 are on disk
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                done = [i for i in range(2, 25)
                        if (scrape.DATA_DIR / f"{self.schemes[i - 1]['id']}.json").exists()]
                if len(done) == 23:
                    break
                time.sleep(0.01)
            others_done_during_wait.append(len(done))

        scrape._sleep = waiting
        self.run_main()
        self.assertEqual(others_done_during_wait, [23, 23], "the other workers were not held up by scheme 1's waits")
        self.assertEqual((self.entry(1)["status"], self.entry(1)["attempts"]), (1, 3))

    def test_every_attempt_gets_its_own_session_and_tls_adapter(self):
        real_attempt = self._saved["attempt_scheme"]
        seen = []

        def fake_once(session, scheme):
            seen.append(session)
            return ok_result(scheme)

        with mock.patch.object(scrape, "_scrape_scheme_once", fake_once):
            real_attempt(self.schemes[0])
            real_attempt(self.schemes[1])
        first, second = seen
        self.assertIsNot(first, second)
        adapter_a, adapter_b = first.adapters["https://"], second.adapters["https://"]
        self.assertIsInstance(adapter_a, scrape.LegacyTLSAdapter)
        self.assertIsNot(adapter_a, adapter_b, "a shared adapter would be torn down under the other workers")
        self.assertFalse(hasattr(scrape, "_LEGACY_TLS_ADAPTER"))

    def test_the_session_is_closed_even_when_the_attempt_fails(self):
        real_attempt = self._saved["attempt_scheme"]
        sessions = []

        def boom(session, scheme):
            sessions.append(session)
            raise TIMEOUT

        with mock.patch.object(scrape, "_scrape_scheme_once", boom), mock.patch.object(requests.Session, "close") as close:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                real_attempt(self.schemes[0])
        close.assert_called_once()


class TestFilesUnderConcurrency(ScraperCase):
    workers = 4

    def test_readers_never_see_a_half_written_json_file(self):
        stop, problems, reads = threading.Event(), [], [0]
        targets = ["crawler_status.json", "index.json"] + [f"{s['id']}.json" for s in self.schemes]

        def reader():
            while not stop.is_set():
                for name in targets:
                    try:
                        text = (scrape.DATA_DIR / name).read_text(encoding="utf-8")
                    except OSError:            # not there yet / momentarily locked (Windows): not a torn file
                        continue
                    reads[0] += 1
                    try:
                        json.loads(text)
                    except ValueError as exc:
                        problems.append(f"{name}: {exc}")

        readers = [threading.Thread(target=reader) for _ in range(2)]
        for t in readers:
            t.start()
        try:
            self.script = {self.code(i): [TIMEOUT] for i in range(1, 25, 3)}
            self.run_main()
        finally:
            stop.set()
            for t in readers:
                t.join()
        self.assertEqual(problems, [])
        self.assertGreater(reads[0], 50, "the readers really did overlap with the run")

    def test_status_writes_are_serialized(self):
        real, lock, inflight = scrape.write_json_atomic, threading.Lock(), {"now": 0, "peak": 0, "writes": 0}

        def spy(path, data):
            if str(path).endswith("crawler_status.json"):
                with lock:
                    inflight["now"] += 1
                    inflight["peak"] = max(inflight["peak"], inflight["now"])
                    inflight["writes"] += 1
                time.sleep(0.002)                        # widen the window a race would need
                try:
                    return real(path, data)
                finally:
                    with lock:
                        inflight["now"] -= 1
            return real(path, data)

        with mock.patch.object(scrape, "write_json_atomic", spy):
            self.run_main()
        self.assertEqual(inflight["peak"], 1, "two threads were writing crawler_status.json at once")
        self.assertGreaterEqual(inflight["writes"], 24 + 2)

    def test_concurrent_status_updates_are_never_lost(self):
        scrape.DATA_DIR.mkdir(parents=True)
        crawl = scrape.CrawlStatus(self.schemes)
        crawl.start()
        threads = [threading.Thread(target=crawl.record, args=(s["scheme_code"], 1, 1, "")) for s in self.schemes]
        with mock.patch.object(scrape, "say"):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        on_disk = self.status()["schemes"]
        self.assertTrue(all(e["status"] == 1 for e in on_disk.values()))
        self.assertEqual(crawl.counts()["successful"], 24)

    def test_index_json_is_only_ever_written_by_the_main_thread(self):
        real, writers = scrape.write_json_atomic, set()

        def spy(path, data):
            if str(path).endswith("index.json"):
                writers.add(threading.current_thread())
            return real(path, data)

        with mock.patch.object(scrape, "write_json_atomic", spy):
            self.run_main()
        self.assertEqual(writers, {threading.main_thread()})

    def test_scheme_data_is_saved_the_moment_it_succeeds_not_at_the_end(self):
        seen_on_disk = {}

        def on_attempt(scheme):
            if scheme["scheme_code"] == self.code(24):          # by the time the last scheme starts, scheme 1 is long done
                seen_on_disk["one"] = (scrape.DATA_DIR / f"{self.schemes[0]['id']}.json").exists()

        self.on_attempt = on_attempt
        self.run_main()
        self.assertTrue(seen_on_disk["one"])

    def test_index_is_rebuilt_as_schemes_complete(self):
        real, counts = scrape.write_index, []

        def spy(schemes, crawl):
            real(schemes, crawl)
            counts.append(self.index()["successful_scheme_count"])

        with mock.patch.object(scrape, "write_index", spy):
            self.run_main()
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[0], 0)
        self.assertEqual(counts[-1], 24)
        self.assertGreaterEqual(len(counts), 24)

    def test_no_temp_files_are_left_behind(self):
        self.script = {self.code(i): [TIMEOUT] for i in range(1, 25, 2)}
        self.run_main()
        self.assertEqual(list(scrape.DATA_DIR.glob("*.tmp")), [])


class TestFatalAndInterrupt(ScraperCase):
    workers = 4

    def _consistent(self):
        st = self.status()
        counts = scrape._counts(st["schemes"].values())
        idx = self.index()
        self.assertEqual((idx["successful_scheme_count"], idx["failed_scheme_count"], idx["unprocessed_scheme_count"]),
                         (counts["successful"], counts["failed"], counts["unprocessed"]))
        self.assertEqual(counts["successful"] + counts["failed"] + counts["unprocessed"], 24)
        for i in range(1, 25):                                   # every success is on disk; nothing claims a success it lacks
            if self.entry(i)["status"] == 1:
                self.assertEqual(self.scheme_file(i)["status"], "ok", f"scheme {i}")
        self.assertEqual(idx["overall_status"], st["overall_status"])
        return st, counts

    def test_a_persistence_failure_in_one_worker_stops_the_run_as_partial(self):
        real_save = scrape.save_result

        def failing_save(scheme, result):
            if scheme["id"] == self.schemes[9]["id"]:
                raise OSError(28, "No space left on device")
            return real_save(scheme, result)

        scrape.save_result = failing_save
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        st, counts = self._consistent()
        self.assertEqual(st["overall_status"], "PARTIAL")
        self.assertIn("No space left on device", st["fatal_error"])
        self.assertEqual(self.entry(10)["status"], 0, "scraped but never persisted: not a success")
        self.assertGreater(counts["unprocessed"], 0)
        self.assertLess(counts["successful"], 24)

    def test_an_interrupt_in_a_worker_winds_the_whole_run_down(self):
        self.script[self.code(6)] = [KeyboardInterrupt("simulated Ctrl-C")]
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        st, counts = self._consistent()
        self.assertEqual(st["overall_status"], "PARTIAL")
        self.assertIn("Interrupted", st["fatal_error"])
        self.assertEqual(self.entry(6)["status"], 0)
        self.assertTrue(scrape._STOP.is_set())

    def test_queued_schemes_are_dropped_not_started_after_a_fatal_error(self):
        gate = threading.Event()

        def on_attempt(scheme):
            if scheme["scheme_code"] == self.code(1):
                gate.wait(5)                                     # hold worker 1 until the failing scheme has failed
                raise KeyboardInterrupt("simulated")

        self.on_attempt = on_attempt
        real_save = scrape.save_result
        scrape.save_result = lambda scheme, result: (_ for _ in ()).throw(OSError(5, "I/O error")) \
            if scheme["id"] == self.schemes[1]["id"] else real_save(scheme, result)
        threading.Timer(0.3, gate.set).start()
        self.assertEqual(self.run_main(), scrape.EXIT_INCOMPLETE)
        st, counts = self._consistent()
        self.assertGreaterEqual(counts["unprocessed"], 10, "the queue behind the failure was never started")


if __name__ == "__main__":
    unittest.main()
