import logging
import unittest
from unittest.mock import patch

from check_jobs import (Job, META, source_version, build_run_report, send_run_email,
                        process, company_report)
from job_filters import geography, matched_keywords, filter_changes


class ReportTests(unittest.TestCase):
    def test_country_codes_and_any_keyword_ignore_case(self):
        self.assertEqual(geography("Bengaluru, KA,IN, IN", "CLOUD"), "India")
        self.assertEqual(matched_keywords("Ansible"), ("ansible",))
        self.assertEqual(matched_keywords("ANSIBLE"), ("ansible",))
        self.assertFalse(matched_keywords("cloudy; windowsill"))
        self.assertFalse(geography("Remote US", "Work from anywhere in the world; Linux."))
        self.assertFalse(geography("Remote", "Linux"))
        self.assertEqual(geography("Remote worldwide", "Linux"), "Worldwide")

    def test_empty_matches_still_generate_email(self):
        previous = {"GitLab": {}, META: {"GitLab": source_version("GitLab")}}
        results = [("GitLab", {"url": Job("url", "Engineer")})]
        with patch("check_jobs.send_email") as smtp:
            send_run_email(results, previous, {}, {})
        smtp.assert_called_once()
        body = smtp.call_args.kwargs["report_body"]
        self.assertIn("No new job matching your required criteria detected", body)
        self.assertIn("0 matching new jobs", smtp.call_args.kwargs["subject"])

    def test_failures_and_empty_extractions_do_not_claim_no_jobs(self):
        subject, body = build_run_report([("GitLab", None), ("Oracle", {})], {}, {}, {})
        self.assertIn("some checks incomplete", subject)
        self.assertIn("CHECK FAILED", body)
        self.assertIn("CHECK INCOMPLETE", body)
        self.assertIn("new-job status unknown", body)
        self.assertNotIn("No new job matching", body)

    def test_baseline_report_and_eligible_job_details(self):
        job = Job("https://example.com/job/1", "DevOps engineer", "Linux", "India", ("linux",), "India")
        results = [("GitLab", {job.url: job})]
        _, body = build_run_report(results, {}, {}, {})
        self.assertIn("BASELINE SAVED", body)
        previous = {"GitLab": {}, META: {"GitLab": source_version("GitLab")}}
        subject, body = build_run_report(results, previous, {"GitLab": [job]},
                                        {"GitLab": {"matched": 1, "pending": 0}})
        self.assertIn("1 matching new jobs", subject)
        self.assertIn("Matched keywords: linux", body)
        self.assertIn(job.url, body)

    def test_pending_descriptions_report_unknown(self):
        previous = {"GitLab": {}, META: {"GitLab": source_version("GitLab")}}
        text = company_report("GitLab", {"url": Job("url", "Role")}, previous,
                              {"GitLab": {"matched": 0, "pending": 1}})
        self.assertIn("No matching new job confirmed yet", text)
        self.assertIn("description check(s) pending", text)


class FilterReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_and_match_counts_and_state_retry(self):
        jobs = [Job("india", "Engineer", "LINUX", "Bengaluru, KA,IN, IN"),
                Job("usa", "Engineer", "AWS", "Remote US"), Job("failed", "Engineer")]
        state = {"GitLab": {job.url: job.title for job in jobs}}
        reports = {}
        async def read(browser, job):
            if job.url == "failed":
                raise RuntimeError("unreadable")
            return job
        with patch("job_filters.read_details", side_effect=read):
            changes = await filter_changes(None, {"GitLab": jobs}, state, logging.getLogger("test"), reports)
        self.assertEqual(len(changes["GitLab"]), 1)
        self.assertEqual(reports["GitLab"], {"candidates": 3, "matched": 1, "skipped": 1, "pending": 1})
        self.assertNotIn("failed", state["GitLab"])
        self.assertIn("usa", state["GitLab"])


if __name__ == "__main__":
    unittest.main()
