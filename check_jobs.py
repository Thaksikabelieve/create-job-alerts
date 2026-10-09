"""Monitor official career sites and email newly discovered job links.

Requires a persistent state/jobs.json file and Chromium installed by Playwright.
All outbound job links must be discovered from the official portal itself.
"""

import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from job_filters import filter_changes, plain_text
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "jobs.json"
PORTALS = json.loads((ROOT / "portals.json").read_text())
LOG = logging.getLogger("career-alerts")
MAX_PAGES = 4  # per company, including landing page
PARALLEL = 5
SCANNER_VERSION = "2"
META = "__source_versions__"
TRACKING = re.compile(r"^(utm_|ref$|source$|src$|gh_src$|lang$|locale$)", re.I)
BOARD_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "eightfold.ai",
    "recruitee.com", "applytojob.com", "teamtailor.com", "workable.com",
    "job-boards.eu.greenhouse.io", "jobs.jobvite.com", "phenompeople.com",
)
JOB_PATHS = [
    re.compile(p, re.I) for p in (
        r"/jobs?/(?:[a-z]{2}/)?(?:\d{3,}(?:[-/][^/?]+)?|[a-f0-9-]{12,})(?:/|$)",
        r"/(?:careers|positions|openings)/jobs?/[^/?]+/[^/?]+",
        r"/(?:careers|job|jobs|positions|openings)/[a-z0-9-]*\d+[a-z0-9-]*(?:/|$)",
        r"/job/[^/?]+/[^/?]+",  # e.g., Workday and IBM
        r"/applications/jobs/results/\d+[-/]",
        r"/en/jobs/\d+",  # Amazon
        r"/global/en/job/[^/?]+",  # Microsoft
        r"/careers-home/jobs/\d+",  # GitHub
        r"/job-details/\d+",
        r"/detail/\d+",  # Datadog
        r"/work-with-us/job/[^/?]+",  # Automattic
        r"/sites/jobsearch/job/\d+",  # Oracle
        r"/jobs/ProjectDetail/[^/?]+/\d+",  # Older Cisco listings
        r"/careers/JobDetail/[^/?]+/\d+",  # IBM
        r"/careerhub/explore/jobs/\d+",  # Microsoft
    )
]
PAGE_CUE = re.compile(r"\b(jobs?|roles?|positions?|openings?|vacancies|opportunities|search|view all|join our team)\b", re.I)
EXCLUDE = re.compile(r"(privacy|terms|cookie|salary|benefits|talent.community|internship.program|job[-_ ]?alerts?|login|sign.in|introduceYourself|\.(?:pdf|zip|png|jpg|svg)$)", re.I)
JOB_ID_KEYS = {"gh_jid", "jobid", "job_id", "job", "reqid", "requisitionid", "jid", "pid", "domain"}


def source_version(company):
    return sha256((SCANNER_VERSION + PORTALS.get(company, "")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Job:
    url: str
    title: str
    description: str = ""
    location: str = ""
    matched: tuple[str, ...] = ()
    eligibility: str = ""


def normalized(url: str) -> str:
    bits = urlsplit(url)
    if bits.scheme not in ("http", "https") or not bits.netloc:
        return ""
    host = bits.hostname.lower().removeprefix("www.") if bits.hostname else ""
    query = urlencode(sorted((k, v) for k, v in parse_qsl(bits.query) if not TRACKING.match(k)))
    return urlunsplit(("https", host, bits.path.rstrip("/") or "/", query, ""))


def is_job(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if EXCLUDE.search(path):
        return False
    if any(pattern.search(path) for pattern in JOB_PATHS):
        return True
    host = (parts.hostname or "").lower()
    params = dict(parse_qsl(parts.query))
    if any(k.lower() == "gh_jid" and v.isdigit() for k, v in params.items()):
        return True
    if host.endswith("pulumi.com") and re.fullmatch(r"/careers/[a-z][a-z0-9-]{10,}", path):
        return True
    if re.search(r"/(?:jobdetails|job-detail|job-search|job)", path, re.I) and any(
        re.fullmatch(r"(?:job|jobid|job_id|jobidof|requisitionid|reqid)", key, re.I)
        and len(value) >= 4 for key, value in params.items()
    ):
        return True
    if host.endswith("greenhouse.io") and re.search(r"/jobs/\d+", path):
        return True
    if host.endswith("lever.co") and re.search(r"/[0-9a-f-]{20,}$", path, re.I):
        return True
    if host.endswith("ashbyhq.com") and re.search(r"/[0-9a-f-]{20,}$", path, re.I):
        return True
    if host.endswith("myworkdayjobs.com") and re.search(r"/job/.+/(?:[A-Z]+[-_])?\d+", path, re.I):
        return True
    return False


def canonical_job_url(url):
    parts = urlsplit(normalized(url))
    # Retain requisition identifiers, discard changing search/tracking parameters.
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() in JOB_ID_KEYS))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def allowed(url: str, original: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    original_host = (urlsplit(original).hostname or "").lower().removeprefix("www.")
    parent = ".".join(original_host.split(".")[-2:])
    # Links on the official site may point at its own pages or recognized ATS providers.
    return host == parent or host.endswith("." + parent) or any(
        host == name or host.endswith("." + name) for name in BOARD_HOSTS
    )


def record(links: list[dict], page_url: str, portal: str) -> tuple[dict[str, Job], list[str]]:
    jobs: dict[str, Job] = {}
    pages: list[tuple[int, str]] = []
    for link in links:
        url = normalized(urljoin(page_url, link.get("href") or ""))
        title = " ".join((link.get("text") or "").split())[:180]
        if not url or not allowed(url, portal):
            continue
        if is_job(url):
            url = canonical_job_url(url)
            jobs[url] = Job(url, title or "Job posting")
        elif PAGE_CUE.search(title + " " + urlsplit(url).path) and not EXCLUDE.search(url):
            priority = 0 if re.search(r"all.jobs|job.openings|search.jobs|open.positions|view.jobs|search-results", title + " " + url, re.I) else 1
            pages.append((priority, url))
    return jobs, [url for _, url in sorted(pages)]


def greenhouse_token(url):
    parts = urlsplit(url)
    if parts.hostname not in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
        return None
    bits = parts.path.strip("/").split("/")
    token = dict(parse_qsl(parts.query)).get("for") if bits[0] == "embed" else bits[0]
    return token if token and re.fullmatch(r"[a-zA-Z0-9_-]+", token) else None


async def greenhouse_jobs(context, token, company):
    try:
        response = await context.request.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", timeout=15000)
        if not response.ok:
            LOG.warning("%s: job-board feed HTTP %s", company, response.status)
            return {}
        data = await response.json()
        if not isinstance(data.get("jobs"), list):
            return {}
        jobs = {}
        for item in data["jobs"]:
            if not item.get("internal_job_id") or not item.get("absolute_url"):
                continue  # Do not alert on talent-pool prospect posts.
            url = canonical_job_url(item["absolute_url"])
            location = (item.get("location") or {}).get("name", "")
            jobs[url] = Job(url, item.get("title", "Job posting") + (f" ({location})" if location else ""),
                            plain_text(item.get("content")), location)
        LOG.info("%s: official Greenhouse feed returned %d jobs", company, len(jobs))
        return jobs
    except Exception as exc:
        LOG.warning("%s: job-board feed: %s", company, exc)
        return {}


async def scan(browser, company: str, portal: str) -> tuple[str, dict[str, Job] | None]:
    context = await browser.new_context(user_agent=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ))
    page = await context.new_page()
    queue = [portal]
    visited = set()
    found: dict[str, Job] = {}
    succeeded = False
    checked_feeds = set()
    successful_feeds = set()
    try:
        while queue and len(visited) < MAX_PAGES:
            target = queue.pop(0)
            if target in visited or is_job(target):
                continue
            visited.add(target)
            try:
                token = greenhouse_token(target)
                if token and token not in checked_feeds:
                    checked_feeds.add(token)
                    feed = await greenhouse_jobs(context, token, company)
                    if feed:
                        successful_feeds.add(token)
                        found.update(feed)
                        succeeded = True
                        continue
                response = await page.goto(target, wait_until="domcontentloaded", timeout=20000)
                if not response or response.status >= 400:
                    LOG.warning("%s: HTTP %s at %s", company, response.status if response else "none", target)
                    continue
                links = []
                last_count = -1
                stable = 0
                # Wait for dynamic listings instead of relying on one fixed delay.
                for attempt in range(8):
                    await page.wait_for_timeout(1000)
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        links = []
                        for frame in page.frames:
                            if allowed(frame.url, portal):
                                links.extend(await frame.locator("a[href]").evaluate_all(
                                    "els => els.map(a => ({href: a.href, text: a.innerText || a.getAttribute('aria-label') || ''}))"
                                ))
                        links.extend(await page.locator("iframe[src]").evaluate_all(
                            "els => els.map(e => ({href:e.src, text:'Open jobs'}))"
                        ))
                        jobs, _ = record(links, page.url, portal)
                        stable = stable + 1 if jobs and len(jobs) == last_count else 0
                        last_count = len(jobs)
                        if stable >= 2:
                            break
                    except Exception:
                        if attempt == 7:
                            raise
                jobs, pages = record(links, page.url, portal)
                found.update(jobs)
                succeeded = True
                for link in links:
                    url = urljoin(page.url, link.get("href") or "")
                    token = greenhouse_token(url) if allowed(url, portal) else None
                    if token and token not in checked_feeds:
                        checked_feeds.add(token)
                        feed = await greenhouse_jobs(context, token, company)
                        found.update(feed)
                        if feed:
                            successful_feeds.add(token)
                for child in pages:
                    if greenhouse_token(child) in successful_feeds:
                        continue
                    if company == "HashiCorp" and dict(parse_qsl(urlsplit(child).query)).get("q", "").lower() != "hashicorp":
                        continue
                    if child not in visited and child not in queue and len(queue) < 15:
                        queue.append(child)
            except Exception as exc:
                LOG.warning("%s: %s: %s", company, target, exc)
        if not succeeded:
            return company, None
        return company, found
    finally:
        await context.close()


def send_email(changes: dict[str, list[Job]], subject: str | None = None, report_body: str | None = None) -> None:
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("ALERT_EMAIL")
    if not all((user, password, recipient)):
        raise RuntimeError("Set SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL before running")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject or f"Career alerts: {sum(map(len, changes.values()))} newly detected jobs"
    body = "\n\n".join(
        company + "\n" + "\n".join(
            f"- {job.title}: {job.url}" +
            (f"\n  Location: {job.location or job.eligibility}; eligible: {job.eligibility}"
             f"\n  Keywords: {', '.join(job.matched)}" if job.matched else "") for job in jobs)
        for company, jobs in sorted(changes.items())
    )
    if subject is None:
        body = "These job links were newly detected by your monitor. Their original posting dates may be earlier.\n\n" + body
    msg.set_content(report_body if report_body is not None else body)
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "465")
    with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def load_state() -> dict[str, dict[str, str]]:
    if not STATE.exists():
        return {}
    data = json.loads(STATE.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid job state")
    return data


def company_report(company, jobs, previous, reports):
    if jobs is None:
        return "CHECK FAILED: portal could not be read; new-job status unknown."
    if not jobs:
        return "CHECK INCOMPLETE: no job links extracted; new-job status unknown."
    if company not in previous or previous.get(META, {}).get(company) != source_version(company):
        return f"BASELINE SAVED: {len(jobs)} existing links recorded; alerts start on later checks."
    report = reports.get(company, {})
    matched = report.get("matched", 0)
    pending = report.get("pending", 0)
    if matched:
        text = f"{matched} new job(s) match your criteria; {len(jobs)} links detected."
    elif pending:
        text = f"No matching new job confirmed yet; {len(jobs)} links detected."
    else:
        text = f"No new job matching your required criteria detected; {len(jobs)} links detected."
    if pending:
        text += f" {pending} description check(s) pending; these will be retried."
    return text


def build_run_report(results, previous, changes, reports):
    count = sum(map(len, changes.values()))
    issues = sum(not jobs for _, jobs in results) + sum(r.get("pending", 0) for r in reports.values())
    outcome = "some checks incomplete" if issues else "portal scans completed"
    subject = f"Career check: {count} matching new jobs — {outcome}"
    checked = datetime.now(timezone.utc).astimezone(
        ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    lines = [f"Your career-check script completed its scanning and filtering steps at {checked}.",
             "This message reports the check even when there are no matching new jobs.",
             "Criteria: India or explicitly worldwide work AND any configured keyword in the description (case-insensitive).",
             "", "COMPANY RESULTS", ""]
    for company, jobs in results:
        lines.append(f"{company}: {company_report(company, jobs, previous, reports)}")
        lines.append(f"Portal: {PORTALS.get(company, '')}")
        for job in changes.get(company, []):
            lines.extend([f"  Job: {job.title}", f"  Location: {job.location or job.eligibility}",
                          f"  Matched keywords: {', '.join(job.matched)}", f"  Link: {job.url}"])
        lines.append("")
    lines.extend(["Coverage is limited to links the scanner can extract; counts do not confirm all company vacancies.",
                  "New means newly detected after baseline, not a verified publication date.",
                  "Unreadable portals and pending descriptions cannot confirm absence of matching jobs.",
                  "Job history is saved by the next workflow step; this email does not confirm that step succeeded."])
    return subject, "\n".join(lines)


def send_run_email(results, previous, changes, reports):
    subject, body = build_run_report(results, previous, changes, reports)
    # Reuse the existing SMTP configuration; the report is sent on every completed check.
    send_email({}, subject=subject, report_body=body)


def process(results: list[tuple[str, dict[str, Job] | None]], previous: dict) -> tuple[dict, dict[str, list[Job]]]:
    updated = {name: dict(jobs) for name, jobs in previous.items()}
    versions = dict(previous.get(META, {}))
    changes: dict[str, list[Job]] = {}
    for company, jobs in results:
        if jobs is None or not jobs:
            LOG.warning("%s: no jobs found; retaining old baseline", company)
            continue
        old_jobs = {canonical_job_url(url): title for url, title in previous.get(company, {}).items()}
        if company in previous and versions.get(company) == source_version(company):
            new = [job for url, job in jobs.items() if url not in old_jobs]
            if new:
                changes[company] = new
        else:
            LOG.info("%s: initial or updated-source baseline of %d jobs (no email)", company, len(jobs))
        updated[company] = {**old_jobs, **{url: job.title for url, job in jobs.items()}}
        versions[company] = source_version(company)
        updated[META] = versions
        LOG.info("%s: found %d jobs, %d new", company, len(jobs), len(changes.get(company, [])))
    return updated, changes


def write_summary(results, previous, changes):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = ["## Career portal results", "", "| Company | Jobs detected | Result |", "|---|---:|---|"]
    for company, jobs in results:
        if jobs is None:
            status = "Could not read portal; check logs"
        elif not jobs:
            status = "No job links detected; check layout or access"
        elif company not in previous or previous.get(META, {}).get(company) != source_version(company):
            status = "Initial or updated-source baseline saved; existing jobs not emailed"
        else:
            status = f"{len(changes.get(company, []))} new jobs match India/worldwide and keyword filters"
        rows.append(f"| {company} | {len(jobs or {})} | {status} |")
    rows.extend(["", "Detected links do not confirm complete coverage. Review failed and empty scans.", ""])
    with open(path, "a") as summary:
        summary.write("\n".join(rows))


async def main() -> None:
    from playwright.async_api import async_playwright

    missing = [
        name for name in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise RuntimeError("Missing or empty repository secrets: " + ", ".join(missing))
    if os.getenv("SEND_TEST_EMAIL", "").lower() == "true":
        send_email({"Setup test": [Job("https://github.com/", "Your email connection works. This is a setup test, not a new job alert.")]},
                   subject="Career alerts: test email")
        LOG.info("Test email accepted by the SMTP server")
    previous = load_state()
    semaphore = asyncio.Semaphore(PARALLEL)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        async def limited(company, portal):
            async with semaphore:
                return await scan(browser, company, portal)
        try:
            results = await asyncio.gather(*(limited(c, u) for c, u in PORTALS.items()))
            updated, candidates = process(results, previous)
            reports = {}
            changes = await filter_changes(browser, candidates, updated, LOG, reports)
        finally:
            await browser.close()
    # Mail first: on SMTP failure the baseline stays old, so a retry is possible.
    send_run_email(results, previous, changes, reports)
    if updated != previous:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(updated, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        temp.replace(STATE)
    write_summary(results, previous, changes)
    LOG.info("Check report accepted by the SMTP server")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
