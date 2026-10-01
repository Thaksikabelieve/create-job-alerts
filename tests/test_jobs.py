import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from check_jobs import Job, is_job, normalized, process, record, send_email, write_summary


class JobCheckTests(unittest.TestCase):
    def test_job_urls_and_category_pages(self):
        self.assertTrue(is_job("https://www.github.careers/careers-home/jobs/5560"))
        self.assertTrue(is_job("https://www.amazon.jobs/en/jobs/2345678/example"))
        self.assertTrue(is_job("https://www.google.com/about/careers/applications/jobs/results/87706967249691334-site-reliability-engineer"))
        self.assertTrue(is_job("https://automattic.com/work-with-us/job/experienced-software-engineer/"))
        self.assertTrue(is_job("https://careers.datadoghq.com/detail/3851935/"))
        self.assertFalse(is_job("https://www.github.careers/careers-home/jobs/categories"))
        self.assertFalse(is_job("https://www.github.careers/careers-home/jobs/locations"))

    def test_links_limited_to_official_and_recognized_ats(self):
        jobs, pages = record([
            {"href": "/careers-home/jobs/5560?utm_source=mail", "text": "Staff Engineer"},
            {"href": "/careers-home/jobs/categories", "text": "Jobs by category"},
            {"href": "https://evil.example/jobs/99999", "text": "Prize"},
        ], "https://www.github.careers/careers-home/", "https://www.github.careers/careers-home/")
        self.assertEqual(list(jobs), ["https://github.careers/careers-home/jobs/5560"])
        self.assertEqual(pages, ["https://github.careers/careers-home/jobs/categories"])

    def test_first_run_and_failed_scans(self):
        first = [("GitHub", {"https://github.careers/careers-home/jobs/1": Job("https://github.careers/careers-home/jobs/1", "First")})]
        state, alerts = process(first, {})
        self.assertEqual(alerts, {})
        failed, alerts = process([("GitHub", None)], state)
        self.assertEqual(failed, state)
        self.assertEqual(alerts, {})
        second = [("GitHub", {"https://github.careers/careers-home/jobs/2": Job("https://github.careers/careers-home/jobs/2", "Second")})]
        state, alerts = process(second, state)
        self.assertEqual([job.title for job in alerts["GitHub"]], ["Second"])
        state, alerts = process(second, state)
        self.assertFalse(alerts)

    def test_tracking_removed_and_job_id_preserved(self):
        self.assertEqual(normalized("https://www.example.com/job?jobId=4321&utm_medium=email"),
                         "https://example.com/job?jobId=4321")

    def test_setup_email_uses_custom_subject_and_recipient(self):
        with patch.dict(os.environ, {"SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "test-password", "ALERT_EMAIL": "recipient@example.com"}), patch("check_jobs.smtplib.SMTP_SSL") as smtp:
            send_email({"Setup": [Job("https://github.com", "Test")]}, subject="Career alerts: test email")
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message["To"], "recipient@example.com")
            self.assertEqual(message["Subject"], "Career alerts: test email")

    def test_summary_distinguishes_failed_empty_and_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                write_summary([("Failed", None), ("Empty", {}), ("Working", {"url": Job("url", "Role")})], {}, {})
            text = summary.read_text()
            self.assertIn("Could not read portal", text)
            self.assertIn("No job links detected", text)
            self.assertIn("First baseline saved", text)


if __name__ == "__main__":
    unittest.main()
