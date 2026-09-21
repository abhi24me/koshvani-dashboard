"""run_daily.sh in a sandbox: a throwaway git repo + bare 'origin', with the real logsafe.py, a stub
scraper (which prints fake secrets on purpose) and a stub mailer.

Nothing here can touch the real repository, remote, .env or docs/data: HOME is pointed at a temp
directory, so the script's $HOME/koshvani-dashboard is the sandbox.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import rmtree_force  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "run_daily.sh").read_bytes().replace(b"\r\n", b"\n")      # a Windows checkout may hold CRLF; bash needs LF
LOGSAFE = (REPO / "logsafe.py").read_bytes()
GITIGNORE = (REPO / ".gitignore").read_bytes()

FAKE_ENV = "GMAIL_SENDER=stub.sender@example.test\nGMAIL_APP_PASSWORD=abcd efgh ijkl mnop\nGMAIL_RECIPIENTS=ops@example.test\n"
LEAKS = ["FAKECOOKIE123456", "FAKEENCTOKEN0123456789abcdefXYZ", "hunter2-not-a-real-password", "abcd efgh ijkl mnop",
         "abcdefghijklmnop", "stub.sender@example.test", "ops@example.test"]

STUB_SCRAPER = textwrap.dedent('''
    import os, sys
    mode = os.environ.get("STUB_MODE", "success")
    print("KOSHVANI CRAWLER START | Execution ID:", os.environ.get("KOSHVANI_EXECUTION_ID"))
    # everything below is deliberately sensitive: none of it may survive into the log or a commit
    j = "".join
    print(j(["Cookie: ASP.NET_SessionId=FAKE", "COOKIE123456; path=/"]))
    print(j(["GET https://koshvani.up.nic.in/Reports/ExpTreas.aspx?enc=FAKE", "ENCTOKEN0123456789abcdefXYZ"]))
    print(j(["connecting with password=hunter2-not-a", "-real-password"]), file=sys.stderr)
    print(j(["debug: mailer login abcd efgh", " ijkl mnop as stub.sender@", "example.test to ops@", "example.test"]), file=sys.stderr)
    print("नमस्ते unicode line survives")
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
        f.write(" ".join(a for a in sys.argv[1:] if not a.replace(".", "").isdigit())
                + f" | pushed={pushed} | id={os.environ.get('KOSHVANI_EXECUTION_ID')} | log={os.environ.get('KOSHVANI_LOG_FILE')}\\n")
    print("stub gmail_alert:", " ".join(sys.argv[1:]))
''')


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash and git")
class RunDailyCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="koshvani_home_"))
        self.addCleanup(rmtree_force, self.home)                # logs are made read-only once published
        self.repo, self.origin, self.alerts = self.home / "koshvani-dashboard", self.home / "origin.git", self.home / "alerts.txt"
        git(self.home, "init", "--bare", "-b", "main", str(self.origin))
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "run_daily.sh").write_bytes(SCRIPT)
        (self.repo / "logsafe.py").write_bytes(LOGSAFE)
        (self.repo / ".gitignore").write_bytes(GITIGNORE)
        (self.repo / "scraper").mkdir()
        (self.repo / "scraper" / "scrape.py").write_text(STUB_SCRAPER, encoding="utf-8")
        (self.repo / "gmail_alert.py").write_text(STUB_ALERT, encoding="utf-8")
        (self.repo / ".env").write_text(FAKE_ENV, encoding="utf-8")          # ignored by git, read by logsafe
        (self.repo / "docs" / "data").mkdir(parents=True)
        (self.repo / "docs" / "data" / "a.json").write_text('{"v": "old"}', encoding="utf-8")
        git(self.repo, "add", "run_daily.sh", "logsafe.py", ".gitignore", "scraper", "gmail_alert.py", "docs")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "remote", "add", "origin", self.origin.as_posix())
        git(self.repo, "push", "-u", "origin", "main")

    def run_script(self, mode):
        env = {**os.environ, "HOME": self.home.as_posix(), "STUB_MODE": mode, "ALERT_LOG": self.alerts.as_posix(),
               "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(["bash", "run_daily.sh"], cwd=self.repo, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        self.output = result.stdout + result.stderr
        return result.returncode

    def alert_calls(self):
        return self.alerts.read_text(encoding="utf-8").splitlines() if self.alerts.exists() else []

    def head(self):
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def origin_git(self, *args):
        return git(self.repo, "--git-dir", self.origin.as_posix(), *args).stdout

    def origin_file(self, path):
        return self.origin_git("show", f"main:{path}")

    def origin_log(self):
        return self.origin_git("log", "--format=%s", "main").splitlines()

    def origin_logs(self):
        return [n for n in self.origin_git("ls-tree", "-r", "--name-only", "main", "logs/").splitlines() if n]

    def local_logs(self):
        return sorted(p.name for p in (self.repo / "logs").glob("daily-*.log"))

    def newest_local_log(self):
        return (self.repo / "logs" / self.local_logs()[-1]).read_text(encoding="utf-8")


class TestEveryRunPublishesItsLog(RunDailyCase):
    def test_a_complete_run_publishes_data_reports_then_publishes_its_log(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertEqual(self.origin_file("docs/data/a.json"), '{"v": "fresh-success"}')
        subjects = self.origin_log()
        self.assertRegex(subjects[0], r"^Add execution log \d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(subjects[1:3], ["Update Koshvani data", "initial"])
        (name,) = self.origin_logs()
        self.assertRegex(name, r"^logs/daily-\d{4}-\d\d-\d\d-\d{6}\.log$")
        published = self.origin_file(name)
        self.assertEqual(published, (self.repo / name).read_text(encoding="utf-8"), "what was pushed is exactly the local file")
        self.assertEqual(len(self.alert_calls()), 1)
        self.assertRegex(self.alert_calls()[0], r"^report --start \| pushed=True \| id=\S+ \| log=logs/daily-")

    def test_the_log_is_complete_and_names_every_step(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        text = self.newest_local_log()
        eid = re.search(r"execution_id=(\S+)", text).group(1)
        for expected in ("KOSHVANI JOB STARTED", f"execution_id={eid} log=logs/daily-", "[1/6] Checking Git status", "[2/6] Pulling",
                         "GIT pull --rebase: START", "GIT pull --rebase: END exit_code=0", "[3/6] Starting Koshvani scraper",
                         "KOSHVANI CRAWLER START", f"Execution ID: {eid}", "SCRAPER END exit_code=0", "[4/6] Checking for data changes",
                         "GIT add data: END exit_code=0", "GIT commit data: END exit_code=0", "GIT push data: END exit_code=0",
                         "GitHub push successful", "[6/6] Validating scheme results and sending the Gmail report",
                         "stub gmail_alert: report --start", "GMAIL report step finished (exit_code=0)", "KOSHVANI JOB COMPLETED",
                         "FINAL STATUS: SUCCESS", "LOG PUBLISH: workflow_exit_code=0", "नमस्ते unicode line survives"):
            self.assertIn(expected, text)
        self.assertRegex(text, r"(?m)^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO  \[run_daily\] ")
        self.assertIn(f"Execution ID: {eid}", self.output, "the same lines also went to the screen")

    def test_the_scraper_and_the_mailer_receive_the_execution_id_and_log_name(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        text = self.newest_local_log()
        eid = re.search(r"execution_id=(\S+)", text).group(1)
        self.assertIn(f"id={eid} | log=logs/{self.local_logs()[-1]}", self.alert_calls()[0])

    def test_a_partial_run_publishes_its_data_and_its_log(self):
        self.assertEqual(self.run_script("partial"), 1)
        self.assertEqual(self.origin_file("docs/data/a.json"), '{"v": "fresh-partial"}')
        self.assertIn("partial run", self.origin_log()[1])
        self.assertRegex(self.origin_log()[0], r"^Add execution log ")
        self.assertEqual(len(self.origin_logs()), 1)
        text = self.origin_file(self.origin_logs()[0])
        for expected in ("the scraper stopped early (exit code 3)", "FINAL STATUS: INCOMPLETE", "KOSHVANI JOB ENDED EARLY"):
            self.assertIn(expected, text)
        self.assertEqual(len(self.alert_calls()), 1)
        self.assertIn("pushed=True", self.alert_calls()[0], "reported from the final status, after the push")

    def test_a_run_with_no_data_changes_still_commits_and_pushes_its_log(self):
        before = self.head()
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertEqual(int(git(self.repo, "rev-list", "--count", f"{before}..HEAD").stdout), 1, "exactly one new commit: the log")
        self.assertRegex(self.origin_log()[0], r"^Add execution log ")
        self.assertEqual(self.origin_log()[1], "initial")
        (name,) = self.origin_logs()
        self.assertIn("FINAL STATUS: SUCCESS - NO DATA CHANGES", self.origin_file(name))
        self.assertEqual(self.origin_git("rev-parse", "main").strip(), self.head(), "origin/main == local main")

    def test_a_partial_run_without_data_changes_publishes_its_log_and_exits_nonzero(self):
        self.assertEqual(self.run_script("partial_nochange"), 1, self.output)
        self.assertEqual(len(self.origin_logs()), 1)
        self.assertIn("FINAL STATUS: INCOMPLETE - scraper exit code 3", self.origin_file(self.origin_logs()[0]))
        self.assertTrue(self.alert_calls()[0].startswith("report"))

    def test_every_run_has_its_own_log_and_earlier_ones_are_never_touched(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        (first,) = self.origin_logs()
        first_blob = self.origin_git("rev-parse", f"main:{first}").strip()
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertEqual(self.run_script("partial_nochange"), 1, self.output)
        logs = self.origin_logs()
        self.assertEqual(len(logs), 3)
        self.assertEqual(len(set(logs)), 3, "no two executions share a log file")
        self.assertIn(first, logs)
        self.assertEqual(self.origin_git("rev-parse", f"main:{first}").strip(), first_blob, "an earlier log is byte-for-byte unchanged")
        ids = [re.search(r"execution_id=(\S+)", self.origin_file(n)).group(1) for n in logs]
        self.assertEqual(len(set(ids)), 3, "and each carries its own execution id")
        for name in logs:                                        # each log holds ONE run only
            self.assertEqual(self.origin_file(name).count("KOSHVANI JOB STARTED"), 1)

    def test_a_published_log_is_read_only_locally(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        path = self.repo / "logs" / self.local_logs()[-1]
        self.assertFalse(os.stat(path).st_mode & stat.S_IWRITE, "a finished execution's log is not editable")

    def test_the_working_tree_ends_clean(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")


class TestNothingSecretIsEverWritten(RunDailyCase):
    def check_clean(self, text, where):
        for leak in LEAKS:
            self.assertNotIn(leak, text, f"{leak!r} leaked into {where}")

    def test_secrets_the_scraper_prints_never_reach_the_log_the_screen_or_a_commit(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        text = self.newest_local_log()
        self.check_clean(text, "the local log")
        self.check_clean(self.output, "the screen")
        self.check_clean(self.origin_file(self.origin_logs()[0]), "the pushed log")
        self.assertIn("<redacted>", text)
        self.assertIn("https://koshvani.up.nic.in/Reports/ExpTreas.aspx?<redacted>", text, "the page stays, the encrypted query goes")
        for rev in self.origin_git("rev-list", "main").split():           # every file of every commit, not only the log
            for name in self.origin_git("ls-tree", "-r", "--name-only", rev).split():
                self.check_clean(self.origin_git("show", f"{rev}:{name}"), f"{name}@{rev[:7]}")

    def test_the_env_file_is_never_committed(self):
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertNotIn(".env", self.origin_git("ls-tree", "-r", "--name-only", "main").split())

    def test_a_leftover_log_with_secrets_is_scrubbed_before_it_is_published(self):
        (self.repo / "logs").mkdir()
        dirty = self.repo / "logs" / "daily-2026-09-01-000000.log"
        dirty.write_text("old run\nCookie: ASP.NET_SessionId=FAKECOOKIE123456\nmailed ops@example.test\nGET https://h.example/x?enc=FAKEENCTOKEN0123456789abcdefXYZ\n",
                         encoding="utf-8")
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertIn("logs/daily-2026-09-01-000000.log", self.origin_logs())
        self.check_clean(self.origin_file("logs/daily-2026-09-01-000000.log"), "the published leftover log")
        self.assertTrue(self.origin_file("logs/daily-2026-09-01-000000.log").startswith("old run\n"))

    def test_a_log_that_still_fails_the_check_is_not_published(self):
        (self.repo / "logs").mkdir()
        (self.repo / "logs" / "daily-2026-09-01-000000.log").write_text("fine\n", encoding="utf-8")
        # make the sanitizer's --scrub-file a no-op so the file stays dirty, while --check still catches it
        source = (self.repo / "logsafe.py").read_text(encoding="utf-8")
        (self.repo / "logsafe.py").write_text(source.replace("def _scrub_file(path):", "def _scrub_file(path):\n    return False\n\n\ndef _unused(path):"),
                                              encoding="utf-8")
        (self.repo / "logs" / "daily-2026-09-01-000000.log").write_text("Cookie: ASP.NET_SessionId=FAKECOOKIE123456\n", encoding="utf-8")
        git(self.repo, "add", "logsafe.py")
        git(self.repo, "commit", "-m", "break scrub-file")
        git(self.repo, "push", "origin", "main")
        self.run_script("success_nochange")
        self.assertNotIn("logs/daily-2026-09-01-000000.log", self.origin_logs())
        self.assertIn("still failed the sanitizer check - NOT publishing it", self.output)


class TestLeftoversAndFailures(RunDailyCase):
    def test_logs_a_previous_run_could_not_publish_go_out_with_the_next_one(self):
        (self.repo / "logs").mkdir()
        (self.repo / "logs" / "daily-2026-09-01-000000.log").write_text("an earlier run that never got pushed\n", encoding="utf-8")
        (self.repo / "logs" / "daily-2026-09-02.log").write_text("legacy single-file daily log\n", encoding="utf-8")
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        logs = self.origin_logs()
        self.assertEqual(len(logs), 3)
        self.assertEqual(self.origin_file("logs/daily-2026-09-01-000000.log"), "an earlier run that never got pushed\n")
        self.assertEqual(self.origin_file("logs/daily-2026-09-02.log"), "legacy single-file daily log\n")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")

    def test_only_intended_files_are_ever_staged(self):
        (self.repo / "notes.txt").write_text("my private scribbles", encoding="utf-8")
        (self.repo / "logs").mkdir()
        (self.repo / "logs" / "unrelated.txt").write_text("not an execution log", encoding="utf-8")
        (self.repo / "docs" / "other.txt").write_text("not data", encoding="utf-8")
        self.assertEqual(self.run_script("success"), 0, self.output)
        names = self.origin_git("ls-tree", "-r", "--name-only", "main").split()
        for stray in ("notes.txt", "logs/unrelated.txt", "docs/other.txt"):
            self.assertNotIn(stray, names)
        self.assertEqual(sorted(git(self.repo, "status", "--porcelain").stdout.split()), ["??", "??", "??", "docs/other.txt", "logs/unrelated.txt", "notes.txt"])

    def test_18_leftovers_of_an_interrupted_run_are_saved_not_discarded(self):
        data = self.repo / "docs" / "data"
        (data / "a.json").write_text('{"v": "scraped-before-the-kill"}', encoding="utf-8")          # modified, uncommitted
        (data / "crawler_status.json").write_text('{\n  "overall_status": "RUNNING"\n}', encoding="utf-8")   # new, untracked
        self.assertEqual(self.run_script("success"), 0, self.output)
        self.assertIn("Saving them as a commit instead of discarding", self.output)
        self.assertIn("previous crawler execution never finished", self.output)
        subjects = self.origin_log()
        self.assertIn("Save partial Koshvani data from an interrupted run", subjects)
        salvaged = self.origin_git("show", f"main~{subjects.index('Save partial Koshvani data from an interrupted run')}:docs/data/a.json")
        self.assertEqual(salvaged, '{"v": "scraped-before-the-kill"}', "the killed run's data is preserved in history")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "", "and the working tree ends clean")

    def test_18_leftover_data_never_blocks_the_pull(self):
        (self.repo / "docs" / "data" / "a.json").write_text('{"v": "leftover"}', encoding="utf-8")
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertNotIn("Git pull failed", self.output)
        self.assertEqual([c for c in self.alert_calls() if c.startswith("error")], [])

    def test_a_failing_pull_alerts_stops_the_scraper_and_still_keeps_its_log(self):
        git(self.repo, "remote", "set-url", "origin", (self.home / "does-not-exist.git").as_posix())
        self.assertEqual(self.run_script("success"), 1)
        calls = [c.split(" | ")[0] for c in self.alert_calls()]
        self.assertEqual(calls[0], "error --start --stage git-pull")   # (the stub drops the epoch value)
        self.assertNotIn("Starting Koshvani scraper", self.output, "the scraper must not run after a failed pull")
        # the log is committed locally even though it cannot be pushed, and the failure of that push is reported
        self.assertIn("error --start --stage log-push", calls)
        self.assertEqual(len(self.local_logs()), 1)
        self.assertRegex(git(self.repo, "log", "-1", "--format=%s").stdout, r"^Add execution log ")
        self.assertIn("FINAL STATUS: ERROR (git pull failed)", self.newest_local_log())
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout.strip(), "")

    def test_a_log_that_could_not_be_pushed_goes_out_with_the_next_run(self):
        git(self.repo, "remote", "set-url", "origin", (self.home / "does-not-exist.git").as_posix())
        self.assertEqual(self.run_script("success"), 1)
        git(self.repo, "remote", "set-url", "origin", self.origin.as_posix())
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertEqual(len(self.origin_logs()), 2, "the first run's log was committed locally and is pushed now")
        self.assertEqual(self.origin_git("rev-parse", "main").strip(), self.head())

    def test_a_push_of_the_log_rejected_because_the_remote_moved_is_retried_after_a_pull(self):
        other = self.home / "other"
        git(self.home, "clone", str(self.origin), str(other))
        for key, value in (("user.name", "Other"), ("user.email", "o@example.test"), ("commit.gpgsign", "false")):
            git(other, "config", key, value)
        hook = textwrap.dedent(f'''
            import subprocess
            def other(*a): subprocess.run(["git", "-C", {other.as_posix()!r}, *a], capture_output=True)
            open({(other / "elsewhere.txt").as_posix()!r}, "w").write("lands while the job runs")
            other("add", "elsewhere.txt"); other("commit", "-m", "someone else"); other("push", "origin", "main")
        ''')
        # the remote moves after the workflow's pull, so the log push at the very end is rejected
        stub = (self.repo / "scraper" / "scrape.py").read_text(encoding="utf-8")
        (self.repo / "scraper" / "scrape.py").write_text(stub.replace("os.makedirs(", hook + "os.makedirs(", 1), encoding="utf-8")
        git(self.repo, "add", "scraper")
        git(self.repo, "commit", "-m", "stub moves the remote")
        git(self.repo, "push", "origin", "main")
        git(other, "pull", "--rebase", "origin", "main")
        self.assertEqual(self.run_script("success_nochange"), 0, self.output)
        self.assertIn("the log push failed - pulling and retrying once", self.output)
        self.assertIn("elsewhere.txt", self.origin_git("ls-tree", "-r", "--name-only", "main").split())
        self.assertEqual(len(self.origin_logs()), 1)
        self.assertEqual(self.origin_git("rev-parse", "main").strip(), self.head())


class TestScriptSource(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.decode("utf-8")

    def test_the_destructive_startup_checkout_is_gone(self):
        self.assertNotIn("git checkout -- docs/data", self.text)
        self.assertNotIn("git reset --hard", self.text)
        self.assertNotIn("git clean", self.text)

    def test_nothing_is_ever_staged_wholesale(self):
        for forbidden in ("git add .", "git add -A", "git add --all", "git add *", "commit -a", "commit --all"):
            self.assertNotIn(forbidden, self.text)
        adds = re.findall(r"git add -- \"\$\{files\[@\]\}\"|git_step \"[^\"]+\" git add [^\n]+", self.text)
        self.assertEqual(len(adds), 3)          # leftover data, data, execution logs - each with explicit paths

    def test_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n"], input=SCRIPT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_whole_script_is_parsed_before_running_so_a_pull_cannot_corrupt_it(self):
        self.assertIn("\nrun_workflow() {\n", self.text)
        self.assertIn("\nrun_outer() {\n", self.text)
        self.assertTrue(self.text.rstrip().endswith("fi\nexit $?"))

    def test_the_workflow_output_is_piped_through_the_sanitizer_into_the_log(self):
        self.assertRegex(self.text, r'KOSHVANI_INNER=1 bash "\$REPO/run_daily.sh" 2>&1 \| python -u "\$REPO/logsafe.py" \| tee -a "\$LOG_FILE"')
        self.assertIn('LOG_FILE="$LOG_DIR/daily-$stamp.log"', self.text)
        self.assertIn("noclobber", self.text, "a log file is created exclusively, never reused")

    def test_the_log_is_published_after_the_workflow_on_every_path(self):
        outer = self.text[self.text.index("run_outer() {"):]
        self.assertLess(outer.index("KOSHVANI_INNER=1"), outer.index("publish_log"))
        self.assertIn("git ls-files --others --exclude-standard", self.text)
        self.assertNotIn("| head", self.text)

    def test_change_detection_sees_untracked_files(self):
        self.assertIn("git status --porcelain -- docs/data", self.text)

    def test_the_scraper_exit_code_is_captured_not_fatal(self):
        self.assertIn("SCRAPER_RC=$?", self.text)
        self.assertNotIn("--stage scraper", self.text)

    def test_only_gmail_is_called(self):
        self.assertIn("gmail_alert.py", self.text)
        self.assertNotRegex(self.text.lower(), "tele" + "gram")


if __name__ == "__main__":
    unittest.main()
