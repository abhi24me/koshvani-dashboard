"""run_daily.sh in a sandbox: a throwaway git repo + bare 'origin', with stub scraper and alert scripts.

Nothing here can touch the real repository, remote, .env or docs/data: HOME is
pointed at a temp directory, so the script's $HOME/koshvani-dashboard is the sandbox.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "run_daily.sh").read_bytes().replace(b"\r\n", b"\n")   # a Windows checkout may hold CRLF; bash needs LF

STUB_SCRAPER = textwrap.dedent('''
    import os, sys
    mode = os.environ.get("STUB_MODE", "success")
    os.makedirs("docs/data", exist_ok=True)
    if mode in ("success", "partial"):
        open("docs/data/a.json", "w").write('{"v": "fresh-%s"}' % mode)
        status = "SUCCESS" if mode == "success" else "PARTIAL"
        open("docs/data/crawler_status.json", "w").write('{"overall_status": "%s"}' % status)
    sys.exit(3 if mode.startswith("partial") else 0)
''')

STUB_ALERT = textwrap.dedent('''
    import os, subprocess, sys
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()
    pushed = git("rev-parse", "HEAD") == git("rev-parse", "origin/main")
    with open(os.environ["ALERT_LOG"], "a") as f:
        f.write(" ".join(a for a in sys.argv[1:] if not a.replace(".", "").isdigit()) + f" | pushed={pushed}\\n")
''')


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash and git")
class RunDailyCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="koshvani_home_"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.repo, self.origin, self.alerts = self.home / "koshvani-dashboard", self.home / "origin.git", self.home / "alerts.txt"
        git(self.home, "init", "--bare", "-b", "main", str(self.origin))
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "run_daily.sh").write_bytes(SCRIPT)
        (self.repo / "scraper").mkdir()
        (self.repo / "scraper" / "scrape.py").write_text(STUB_SCRAPER, encoding="utf-8")
        (self.repo / "telegram_alert.py").write_text(STUB_ALERT, encoding="utf-8")
        (self.repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
        (self.repo / "docs" / "data").mkdir(parents=True)
        (self.repo / "docs" / "data" / "a.json").write_text('{"v": "old"}', encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "remote", "add", "origin", self.origin.as_posix())
        git(self.repo, "push", "-u", "origin", "main")

    def run_script(self, mode):
        env = {**os.environ, "HOME": self.home.as_posix(), "STUB_MODE": mode, "ALERT_LOG": self.alerts.as_posix()}
        result = subprocess.run(["bash", "run_daily.sh"], cwd=self.repo, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=240)
        self.output = result.stdout + result.stderr
        return result.returncode

    def alert_calls(self):
        return self.alerts.read_text(encoding="utf-8").splitlines() if self.alerts.exists() else []

    def head(self):
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def origin_file(self, path):
        return git(self.repo, "--git-dir", self.origin.as_posix(), "show", f"main:{path}").stdout

    def origin_log(self):
        return git(self.repo, "--git-dir", self.origin.as_posix(), "log", "--format=%s", "main").stdout.splitlines()


class TestRunDaily(RunDailyCase):
    def test_a_complete_run_publishes_and_reports_after_the_push(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertEqual(self.origin_file("docs/data/a.json"), '{"v": "fresh-success"}')
        self.assertEqual(self.origin_log()[0], "Update Koshvani data")
        self.assertEqual(self.alert_calls(), ["report --start | pushed=True"], "one report, sent only after the push")
        self.assertIn("Status: SUCCESS", self.output)

    def test_18_a_scraper_that_stops_early_does_not_discard_its_partial_data(self):
        rc = self.run_script("partial")
        self.assertEqual(rc, 1, "the job is reported as incomplete")
        self.assertEqual(self.origin_file("docs/data/a.json"), '{"v": "fresh-partial"}', "the partial data was committed AND pushed")
        self.assertIn('"PARTIAL"', self.origin_file("docs/data/crawler_status.json"))
        self.assertIn("partial run", self.origin_log()[0])
        self.assertEqual(self.alert_calls(), ["report --start | pushed=True"],
                         "reported from the final status - not the old blanket 'scraper failed' error")
        self.assertIn("stopped early (exit code 3)", self.output)
        self.assertIn("Status: INCOMPLETE", self.output)

    def test_18_nothing_to_publish_after_an_early_stop_still_reports_and_exits_nonzero(self):
        before = self.head()
        self.assertEqual(self.run_script("partial_nochange"), 1, self.output)
        self.assertEqual(self.head(), before, "no commit when nothing changed")
        self.assertEqual(len(self.alert_calls()), 1)
        self.assertTrue(self.alert_calls()[0].startswith("report"))

    def test_18_leftovers_of_an_interrupted_run_are_saved_not_discarded(self):
        data = self.repo / "docs" / "data"
        (data / "a.json").write_text('{"v": "scraped-before-the-kill"}', encoding="utf-8")          # modified, uncommitted
        (data / "crawler_status.json").write_text('{\n  "overall_status": "RUNNING"\n}', encoding="utf-8")   # new, untracked
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertIn("Saving them as a commit instead of discarding", self.output)
        self.assertIn("previous crawler execution never finished", self.output)
        subjects = self.origin_log()
        self.assertIn("Save partial Koshvani data from an interrupted run", subjects)
        salvaged = git(self.repo, "--git-dir", self.origin.as_posix(), "show",
                       f"main~1:docs/data/a.json").stdout
        self.assertEqual(salvaged, '{"v": "scraped-before-the-kill"}', "the killed run's data is preserved in history")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "", "and the working tree ends clean")
        self.assertEqual(self.alert_calls(), ["report --start | pushed=True"])

    def test_18_leftover_data_never_blocks_the_pull(self):
        (self.repo / "docs" / "data" / "a.json").write_text('{"v": "leftover"}', encoding="utf-8")
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertNotIn("Git pull failed", self.output)
        self.assertEqual([c for c in self.alert_calls() if c.startswith("error")], [])

    def test_a_failing_pull_still_alerts_and_stops(self):
        git(self.repo, "remote", "set-url", "origin", (self.home / "does-not-exist.git").as_posix())
        self.assertEqual(self.run_script("success"), 1)
        self.assertEqual(self.alert_calls()[0].split(" | ")[0], "error --start --stage git-pull")   # (the stub drops the epoch value)
        self.assertNotIn("Starting Koshvani scraper", self.output, "the scraper must not run after a failed pull")


class TestScriptSource(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.decode("utf-8")

    def test_the_destructive_startup_checkout_is_gone(self):
        self.assertNotIn("git checkout -- docs/data", self.text)
        self.assertNotIn("git reset --hard", self.text)
        self.assertNotIn("git clean", self.text)

    def test_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n"], input=SCRIPT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_whole_script_lives_in_main_so_a_pull_cannot_corrupt_it(self):
        self.assertIn("\nmain() {\n", self.text)
        self.assertTrue(self.text.rstrip().endswith('main "$@"\nexit $?'))

    def test_change_detection_sees_untracked_files(self):
        self.assertIn("git status --porcelain -- docs/data", self.text)

    def test_the_scraper_exit_code_is_captured_not_fatal(self):
        self.assertIn("SCRAPER_RC=$?", self.text)
        self.assertNotIn("--stage scraper", self.text)


if __name__ == "__main__":
    unittest.main()
