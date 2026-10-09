import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

from check_jobs import Job, META, is_job, normalized, process, record, send_email, write_summary, canonical_job_url, allowed, greenhouse_token, greenhouse_jobs


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
            self.assertIn("updated-source baseline saved", text)

    def test_pdf_and_job_alerts_are_not_crawled(self):
        jobs, pages = record([
            {"href": "https://careers.google.com/jobs/dist/legal/example.pdf", "text": "Jobs legal notice"},
            {"href": "https://careers.google.com/jobs/JobAlerts", "text": "Job alerts"},
        ], "https://www.google.com/about/careers/", "https://www.google.com/about/careers/")
        self.assertFalse(jobs)
        self.assertFalse(pages)

    def test_job_query_id_and_sibling_domains(self):
        url = "https://www.digitalocean.com/careers/position?gh_jid=123456&utm_source=email"
        self.assertTrue(is_job(url))
        self.assertEqual(canonical_job_url(url), "https://digitalocean.com/careers/position?gh_jid=123456")
        self.assertTrue(allowed("https://careers.cisco.com/global/en", "https://jobs.cisco.com/"))
        self.assertFalse(allowed("https://evilcisco.com/job/1234", "https://jobs.cisco.com/"))

    def test_tracking_changes_do_not_change_job_identity(self):
        self.assertEqual(
            canonical_job_url("https://careers.ibm.com/job/Paris/Engineer/12345?recommendation=abc"),
            canonical_job_url("https://careers.ibm.com/job/Paris/Engineer/12345?recommendation=xyz")
        )

    def test_upgrade_rebaselines_without_alerting_old_jobs(self):
        previous = {"GitLab": {"https://example.com/jobs/123": "Old"}}
        jobs = {"https://example.com/jobs/456": Job("https://example.com/jobs/456", "Newly visible")}
        state, changes = process([("GitLab", jobs)], previous)
        self.assertFalse(changes)
        self.assertIn("GitLab", state[META])
        jobs["https://example.com/jobs/789"] = Job("https://example.com/jobs/789", "Next job")
        _, changes = process([("GitLab", jobs)], state)
        self.assertEqual([j.title for j in changes["GitLab"]], ["Next job"])

    def test_greenhouse_board_token(self):
        self.assertEqual(greenhouse_token("https://job-boards.greenhouse.io/grafanalabs/jobs/12345"), "grafanalabs")
        self.assertEqual(greenhouse_token("https://boards.greenhouse.io/embed/job_board?for=example"), "example")
        self.assertIsNone(greenhouse_token("https://example.com/grafanalabs"))


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_uses_job_post_ids_and_excludes_talent_pool(self):
        response = AsyncMock()
        response.ok = True
        response.json.return_value = {"jobs": [
            {"id":1234, "internal_job_id":4567, "title":"Engineer",
             "location":{"name":"India"}, "absolute_url":"https://job-boards.greenhouse.io/example/jobs/1234?gh_src=email"},
            {"id":9999, "internal_job_id":None, "title":"Talent pool",
             "absolute_url":"https://job-boards.greenhouse.io/example/jobs/9999"},
        ]}
        context = AsyncMock()
        context.request.get.return_value = response
        jobs = await greenhouse_jobs(context, "example", "Example")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(next(iter(jobs.values())).title, "Engineer (India)")
        self.assertEqual(next(iter(jobs)), "https://job-boards.greenhouse.io/example/jobs/1234")
        context.request.get.assert_awaited_once_with(
            "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true", timeout=15000)


if __name__ == "__main__":
    unittest.main()
