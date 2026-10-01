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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "jobs.json"
PORTALS = json.loads((ROOT / "portals.json").read_text())
LOG = logging.getLogger("career-alerts")
MAX_PAGES = 4  # per company, including landing page
PARALLEL = 5
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
        r"/(?:careers|job|jobs|positions|openings)/(?:[a-z0-9-]*\d+[a-z0-9-]*|[a-z][a-z0-9-]{12,})(?:/|$)",
        r"/job/[^/?]+/[^/?]+",  # e.g., Workday and IBM
        r"/applications/jobs/results/\d+[-/]",
        r"/en/jobs/\d+",  # Amazon
        r"/global/en/job/[^/?]+",  # Microsoft
        r"/careers-home/jobs/\d+",  # GitHub
        r"/job-details/\d+",
        r"/detail/\d+",  # Datadog
        r"/work-with-us/job/[^/?]+",  # Automattic
        r"/sites/jobsearch/job/\d+",  # Oracle
    )
]
PAGE_CUE = re.compile(r"\b(jobs?|roles?|positions?|openings?|vacancies|opportunities|search|view all|join our team)\b", re.I)
EXCLUDE = re.compile(r"(privacy|terms|cookie|salary|benefits|talent.community|internship.program|job.alert|login|sign.in)", re.I)


@dataclass(frozen=True)
class Job:
    url: str
    title: str


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


def allowed(url: str, original: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    parent = (urlsplit(original).hostname or "").lower().removeprefix("www.")
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
            jobs[url] = Job(url, title or "Job posting")
        elif PAGE_CUE.search(title + " " + urlsplit(url).path) and not EXCLUDE.search(url):
            priority = 0 if re.search(r"all.jobs|job.openings|search.jobs|open.positions|view.jobs", title + " " + url, re.I) else 1
            pages.append((priority, url))
    return jobs, [url for _, url in sorted(pages)]


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
    try:
        while queue and len(visited) < MAX_PAGES:
            target = queue.pop(0)
            if target in visited or is_job(target):
                continue
            visited.add(target)
            try:
                response = await page.goto(target, wait_until="domcontentloaded", timeout=20000)
                if not response or response.status >= 400:
                    LOG.warning("%s: HTTP %s at %s", company, response.status if response else "none", target)
                    continue
                await page.wait_for_timeout(1500)
                # Some ATS listings lazily load only after the first scroll.
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
                links = await page.locator("a[href]").evaluate_all(
                    "els => els.map(a => ({href: a.href, text: a.innerText || a.getAttribute('aria-label') || ''}))"
                )
                jobs, pages = record(links, page.url, portal)
                found.update(jobs)
                succeeded = True
                for child in pages:
                    if child not in visited and child not in queue and len(queue) < 15:
                        queue.append(child)
            except Exception as exc:
                LOG.warning("%s: %s: %s", company, target, exc)
        if not succeeded:
            return company, None
        return company, found
    finally:
        await context.close()


def send_email(changes: dict[str, list[Job]], subject: str | None = None) -> None:
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("ALERT_EMAIL")
    if not all((user, password, recipient)):
        raise RuntimeError("Set SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL before running")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject or f"New jobs: {sum(map(len, changes.values()))} across {len(changes)} companies"
    msg.set_content("\n\n".join(
        company + "\n" + "\n".join(f"- {job.title}: {job.url}" for job in jobs)
        for company, jobs in sorted(changes.items())
    ))
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


def process(results: list[tuple[str, dict[str, Job] | None]], previous: dict) -> tuple[dict, dict[str, list[Job]]]:
    updated = {name: dict(jobs) for name, jobs in previous.items()}
    changes: dict[str, list[Job]] = {}
    for company, jobs in results:
        if jobs is None or not jobs:
            LOG.warning("%s: no jobs found; retaining old baseline", company)
            continue
        if company in previous:
            new = [job for url, job in jobs.items() if url not in previous[company]]
            if new:
                changes[company] = new
        else:
            LOG.info("%s: initial baseline of %d jobs (no email)", company, len(jobs))
        updated[company] = {**updated.get(company, {}), **{url: job.title for url, job in jobs.items()}}
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
        elif company not in previous:
            status = "First baseline saved; existing jobs not emailed"
        else:
            status = f"{len(changes.get(company, []))} new links found"
        rows.append(f"| {company} | {len(jobs or {})} | {status} |")
    rows.extend(["", "Detected links do not confirm complete coverage. Review failed and empty scans.", ""])
    with open(path, "a") as summary:
        summary.write("\n".join(rows))


async def main() -> None:
    from playwright.async_api import async_playwright

    if not all(os.getenv(key) for key in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL")):
        raise RuntimeError("Configure SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL as repository secrets")
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
        finally:
            await browser.close()
    updated, changes = process(results, previous)
    # Mail first: on SMTP failure the baseline stays old, so a retry is possible.
    if changes:
        send_email(changes)
    if updated != previous:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps(updated, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        temp.replace(STATE)
    write_summary(results, previous, changes)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
