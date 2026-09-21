"""Repository-level guarantees: the retired chat integration is gone for good, execution logs are tracked (not
ignored) and clean, and no credential can be sitting in a tracked file."""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO  # noqa: E402
import logsafe  # noqa: E402

WORD = "tele" + "gram"               # spelled in two halves so this file does not trip its own check
SKIP_DIRS = {".git", "__pycache__", ".venv", "logs", ".koshvani_previous_data", "node_modules"}
NEW_STYLE_LOG = re.compile(r"^daily-\d{4}-\d\d-\d\d-\d{6}(-\d+)?\.log$")


def repo_files():
    for path in REPO.rglob("*"):
        if path.is_file() and not (SKIP_DIRS & set(path.relative_to(REPO).parts)) and path.name != ".env":
            yield path


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8")


class TestRetiredChannelIsGone(unittest.TestCase):
    def test_no_source_config_or_doc_file_mentions_it(self):
        offenders = []
        for path in repo_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue                                      # binary
            if WORD in text.lower():
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "the integration must be removed everywhere (old execution logs are immutable history)")

    def test_the_module_was_renamed_and_the_old_one_is_gone(self):
        self.assertTrue((REPO / "gmail_alert.py").exists())
        self.assertFalse((REPO / f"{WORD}_alert.py").exists())
        self.assertEqual([p.name for p in REPO.rglob(f"*{WORD}*") if not (SKIP_DIRS & set(p.relative_to(REPO).parts))], [])

    def test_the_mailer_has_no_http_client_and_no_channel_other_than_smtp(self):
        source = (REPO / "gmail_alert.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import requests|from requests)")
        self.assertNotIn("api.", source)
        self.assertIn("smtplib", source)

    def test_no_new_dependency_was_introduced(self):
        requirements = (REPO / "scraper" / "requirements.txt").read_text(encoding="utf-8").lower()
        names = {re.split(r"[<>=!~\[ ]", line.strip())[0] for line in requirements.splitlines() if line.strip() and not line.startswith("#")}
        self.assertLessEqual(names, {"requests", "beautifulsoup4", "lxml", "certifi", "urllib3", "idna", "charset-normalizer"})

    def test_run_daily_calls_only_the_gmail_module(self):
        script = (REPO / "run_daily.sh").read_text(encoding="utf-8")
        self.assertIn("gmail_alert.py", script)
        self.assertEqual(len(re.findall(r"python \S*\b\w+_alert\.py", script)), len(re.findall(r"gmail_alert\.py", script)))


@unittest.skipUnless(shutil.which("git"), "needs git")
class TestLogsAreTracked(unittest.TestCase):
    def test_logs_are_not_ignored(self):
        self.assertNotRegex((REPO / ".gitignore").read_text(encoding="utf-8"), r"(?m)^\s*/?logs/?\*?\s*$")
        for name in ("logs/daily-2026-09-22-120000.log", "logs/daily-2026-09-22-120000-2.log", "logs/daily-2026-09-22.log"):
            result = git("check-ignore", "-q", name)
            self.assertEqual(result.returncode, 1, f"{name} must not be ignored")

    def test_secrets_and_local_state_are_ignored(self):
        for name in (".env", ".koshvani_previous_data/x.json"):
            self.assertEqual(git("check-ignore", "-q", name).returncode, 0, f"{name} must stay ignored")

    def test_logs_that_exist_in_git_stay_tracked(self):
        tracked = git("ls-files", "logs").stdout.split()
        on_disk = sorted(f"logs/{p.name}" for p in (REPO / "logs").glob("daily-*.log")) if (REPO / "logs").exists() else []
        untracked = [f for f in on_disk if f not in tracked]
        # A log that has not been committed yet is fine on a developer machine (run_daily.sh commits it), but it must be visible to git.
        for name in untracked:
            self.assertEqual(git("check-ignore", "-q", name).returncode, 1, f"{name} is invisible to git")

    def test_every_execution_log_in_the_repo_passes_the_sanitizer(self):
        logsafe.configure(REPO / ".env")
        self.addCleanup(logsafe.configure, None)
        bad = []
        for path in sorted((REPO / "logs").glob("daily-*.log")) if (REPO / "logs").exists() else []:
            if NEW_STYLE_LOG.match(path.name):                # the per-execution logs written from now on
                text = path.read_text(encoding="utf-8", errors="replace")
                if logsafe.scrub(text) != text:
                    bad.append(path.name)
        self.assertEqual(bad, [])


@unittest.skipUnless(shutil.which("git"), "needs git")
class TestNoSecretsInTheRepository(unittest.TestCase):
    def test_no_value_from_the_local_env_appears_in_any_file_git_knows_about(self):
        env = REPO / ".env"
        if not env.exists():
            self.skipTest("no local .env to compare against")
        secrets = set()
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                value = line.partition("=")[2].strip().strip('"').strip("'")
                secrets.update(p.strip() for p in value.split(",") if len(p.strip()) >= 6)
                secrets.add("".join(value.split()))
        secrets = {s for s in secrets if len(s) >= 6}
        files = set(git("ls-files").stdout.splitlines()) | set(git("ls-files", "--others", "--exclude-standard").stdout.splitlines())
        offenders = []
        for name in sorted(files):
            path = REPO / name
            if not path.is_file() or name == ".env":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if any(s in text for s in secrets):
                offenders.append(name)
        self.assertEqual(offenders, [], "a credential is present in a file that would be committed")

    def test_the_scraper_never_logs_a_url_with_a_query_string(self):
        source = (REPO / "scraper" / "scrape.py").read_text(encoding="utf-8")
        for match in re.finditer(r"say\(f?\"[^\"\n]*\{(?:url|r\.url|response\.url)\}", source):
            self.fail(f"a raw URL is logged: {match.group(0)}")


if __name__ == "__main__":
    unittest.main()
