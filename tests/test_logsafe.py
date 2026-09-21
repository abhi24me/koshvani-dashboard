"""logsafe.py: the sanitizer every log line, the log file and (as a last line of defence) every commit goes through."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import REPO  # noqa: E402
import logsafe  # noqa: E402

R = logsafe.REDACTED
ENC = "Zk9x3QvT8rLm2Pq7WnYb4Hc6JdSa1UeVgXi0RoNt5AwBfKyMlCzDhEjIpG"


class LogsafeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="koshvani_logsafe_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.env = self.tmp / ".env"
        self.env.write_text("GMAIL_SENDER=alerts.sender@example.org\nGMAIL_APP_PASSWORD=abcd efgh ijkl mnop\n"
                            "GMAIL_RECIPIENTS=boss@example.org, team@example.org\nFLAG=true\nN=4\n", encoding="utf-8")
        logsafe.configure(self.env)
        self.addCleanup(logsafe.configure, None)


class TestScrub(LogsafeCase):
    def test_env_values_are_removed_including_split_and_respaced_forms(self):
        for secret in ("alerts.sender@example.org", "abcd efgh ijkl mnop", "abcdefghijklmnop", "boss@example.org", "team@example.org"):
            self.assertNotIn(secret, logsafe.scrub(f"x {secret} y"))

    def test_short_env_values_do_not_mangle_ordinary_text(self):
        self.assertEqual(logsafe.scrub("workers=4 flag true on port 443"), "workers=4 flag true on port 443")

    def test_url_query_strings_and_credentials_are_dropped(self):
        out = logsafe.scrub(f"GET https://user:pw123@koshvani.up.nic.in/Reports/ExpTreas.aspx?enc={ENC}&x=1 done")
        self.assertEqual(out, f"GET https://koshvani.up.nic.in/Reports/ExpTreas.aspx?{R} done")

    def test_cookies_and_authorization_headers(self):
        self.assertEqual(logsafe.scrub("Cookie: ASP.NET_SessionId=abc123; other=1"), f"Cookie: {R}")
        self.assertEqual(logsafe.scrub("Set-Cookie: sid=xyz; HttpOnly"), f"Set-Cookie: {R}")
        self.assertNotIn("s3cr3t", logsafe.scrub("Authorization: Bearer s3cr3tvalue-xyz"))
        self.assertNotIn("s3cr3t", logsafe.scrub("authorization=Basic s3cr3tvalue"))

    def test_key_value_secrets(self):
        for text in ("password=hunter2", "PASSWORD: hunter2", "api_key=hunter2", "apikey='hunter2'", "token=hunter2&x=1", 'secret: "hunter2"',
                     "enc=hunter2", "__VIEWSTATE=hunter2"):
            self.assertNotIn("hunter2", logsafe.scrub(text), text)

    def test_known_credential_formats(self):
        for token in ("ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4", "github_pat_" + "A" * 30, "123456789:" + "A" * 35, "AKIA" + "A" * 16,
                      "xoxb-" + "1234567890-abcdef", "sk-" + "a" * 30, "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4"):
            self.assertNotIn(token, logsafe.scrub(f"leaked {token} here"))

    def test_long_opaque_tokens_go_but_git_shas_paths_and_ids_stay(self):
        self.assertEqual(logsafe.scrub(f"blob {ENC} end"), f"blob {R} end")
        sha = "098ad5f" + "0" * 33
        self.assertEqual(logsafe.scrub(f"commit {sha}"), f"commit {sha}")
        sha256 = "a1" * 32
        self.assertEqual(logsafe.scrub(f"object {sha256}"), f"object {sha256}")
        for keep in ("agri-011-2401000000001.json", "logs/daily-2026-09-22-120000.log", "S:\\Bhaiya\\Koshvani_Report\\docs\\data",
                     "20260922-120000-abc123", "2401000000001"):
            self.assertEqual(logsafe.scrub(keep), keep)

    def test_e_mail_addresses_and_private_keys(self):
        self.assertEqual(logsafe.scrub("mail someone.else@corp.example.com now"), "mail <email> now")
        self.assertNotIn("MIIB", logsafe.scrub("-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkq\n-----END PRIVATE KEY-----"))

    def test_normal_log_lines_pass_through_untouched(self):
        for line in ("2026-09-22 01:02:03.456 INFO  [worker_1] [03/24 2401000000003] HTTP GET grant list (ExpGrant.aspx) END status=200 bytes=18423 elapsed=1.42s",
                     "STATUS UPDATE scheme=2401000000003 status=-1 attempts=2 remark='ReadTimeout: HTTPSConnectionPool(host=koshvani.up.nic.in, port=443): Read timed out.'",
                     "Wall time: 154.2s | workers: 4 | max attempts per scheme: 3",
                     "[5/6] Committing and pushing: Update Koshvani data"):
            self.assertEqual(logsafe.scrub(line), line)

    def test_scrubbing_is_idempotent(self):
        text = (f"Cookie: a=b\nGET https://koshvani.up.nic.in/x.aspx?enc={ENC}\npassword=hunter2 boss@example.org {ENC}\n"
                "https://koshvani.up.nic.in/x.aspx?\nkeep 098ad5f" + "0" * 33)
        once = logsafe.scrub(text)
        self.assertEqual(logsafe.scrub(once), once)

    def test_empty_and_non_string_input(self):
        self.assertEqual(logsafe.scrub(""), "")
        self.assertIsNone(logsafe.scrub(None))
        self.assertEqual(logsafe.scrub(12345), "12345")

    def test_secret_looking_process_variables_are_redacted_too(self):
        os.environ["KOSHVANI_TEST_API_TOKEN"] = "process-env-secret-value"
        try:
            logsafe.configure(self.env)
            self.assertNotIn("process-env-secret-value", logsafe.scrub("value process-env-secret-value here"))
        finally:
            del os.environ["KOSHVANI_TEST_API_TOKEN"]
            logsafe.configure(self.env)


class TestCommandLine(LogsafeCase):
    def run_cli(self, *args, stdin=None):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(REPO / "logsafe.py"), *args], input=stdin, capture_output=True, env=env, timeout=60)

    def test_stream_filter_sanitizes_line_by_line_and_keeps_unicode(self):
        secret_line = f"see https://koshvani.up.nic.in/a.aspx?enc={ENC} and password=hunter2\n"
        proc = self.run_cli(stdin=("नमस्ते ok\n" + secret_line + "last line without newline").encode("utf-8"))
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("नमस्ते ok\n", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn(ENC, out)
        self.assertTrue(out.endswith("last line without newline"))

    def test_scrub_file_rewrites_in_place_and_check_confirms(self):
        log = self.tmp / "daily.log"
        log.write_text(f"ok line\nCookie: sid=abc\nGET https://h.example/x?enc={ENC}\n", encoding="utf-8")
        self.assertEqual(self.run_cli("--check", str(log)).returncode, 1, "a dirty file fails the check")
        proc = self.run_cli("--scrub-file", str(log))
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"sanitized in place", proc.stderr)
        text = log.read_text(encoding="utf-8")
        self.assertNotIn("sid=abc", text)
        self.assertNotIn(ENC, text)
        self.assertTrue(text.startswith("ok line\n"))
        self.assertEqual(self.run_cli("--check", str(log)).returncode, 0)
        self.assertIn(b"already clean", self.run_cli("--scrub-file", str(log)).stderr)
        self.assertEqual(list(self.tmp.glob("*.scrub.tmp")), [])

    def test_the_cli_uses_the_repo_env_when_present(self):
        proc = self.run_cli(stdin=b"mail to boss@example.org from alerts.sender@example.org\n")
        self.assertEqual(proc.returncode, 0)          # (no .env of the test's own is visible to the subprocess; e-mails still go)
        self.assertNotIn(b"boss@example.org", proc.stdout)


if __name__ == "__main__":
    unittest.main()
