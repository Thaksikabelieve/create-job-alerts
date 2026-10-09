"""Conservative geography and description filters for job alerts."""
import json
import re
from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

KEYWORDS = json.loads((Path(__file__).parent / "filters.json").read_text())["keywords"]



class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    parser = TextParser()
    parser.feed(unescape(unescape(str(value or ""))))
    return " ".join(" ".join(parser.parts).split())


def matched_keywords(description):
    return tuple(word for word in KEYWORDS if re.search(
        r"(?<!\w)" + r"\s+".join(re.escape(part) for part in word.split())
        + (r"s?(?!\w)" if word.lower() == "playbook" else r"(?!\w)"),
        description, re.I))


def geography(location, description):
    # Use job location fields, not India mentioned among a company's offices.
    # Country codes occur inside flattened address fields, e.g. Bengaluru, KA,IN, IN.
    india_code = re.search(r"(?:^|[,;|])\s*IN\s*(?=[,;|]|$)", location)
    if (re.search(r"\bIndia\b", location, re.I) or india_code) and not re.search(r"\b(?:excluding|except|outside|not)\b", location, re.I):
        return "India"
    global_location = re.search(
        r"\b(worldwide|global|anywhere in the world|work from anywhere)\b", location, re.I)
    global_location = global_location or re.fullmatch(r"(?:remote\s*[-:,]?\s*)?anywhere", location.strip(), re.I)
    # A geographic restriction overrides a generic global slogan.
    restricted = re.search(r"\b(US|USA|United States|Canada|UK|Europe|EMEA|APAC|Australia|only|restricted)\b", location, re.I)
    restricted = restricted or re.search(
        r"\b(?:US|USA|United States|Canada|UK|Europe|EMEA|APAC|Australia)[ -]only\b|"
        r"\b(?:you|applicants|candidates) must (?:be |reside |live )?(?:based |located )?(?:in |within )"
        r"(?:the )?(?:US|USA|United States|Canada|UK|Europe|EMEA|APAC|Australia)\b",
        description, re.I)
    if global_location and not restricted:
        return "Worldwide"
    # Only explicit applicant/work eligibility statements count in the description.
    for sentence in re.split(r"[.!?\n]", description):
        if re.search(r"\b(?:(?:you|applicants|candidates) (?:must |may |can |should |are |be |already )*(?:work|working|based|located|reside|residing) (?:in |from )India|(?:this |the )?(?:role|position|job) (?:is )?(?:based|located) in India|(?:applicants|candidates) from India)\b", sentence, re.I):
            if not re.search(r"\b(?:not|cannot|can't|ineligible|except|excluding|outside)\b", sentence, re.I):
                return "India"
        generic_remote = not location.strip() or bool(re.fullmatch(r"(?:fully\s+)?remote", location.strip(), re.I))
        if generic_remote and not restricted and re.search(
            r"\b(?:work(?:ing)? (?:remotely )?from anywhere(?: in the world)?|"
            r"(?:applicants|candidates) (?:from |based )?(?:anywhere in the world|worldwide)|"
            r"(?:this (?:role|position|job) is|location:) (?:fully )?remote worldwide)\b",
            sentence, re.I) and not re.search(r"\b(?:not|except|excluding|only|restricted|US|USA|UK|Europe|EMEA|APAC|Canada|Australia)\b", sentence, re.I):
            return "Worldwide"
    return ""


def address_text(value):
    if isinstance(value, list):
        return "; ".join(address_text(item) for item in value)
    if isinstance(value, dict):
        if "address" in value:
            return address_text(value["address"])
        value = dict(value)
        if value.get("addressCountry") == "IN":
            value["addressCountry"] = "India"
        if value.get("@type") == "Country" and value.get("name") == "IN":
            value["name"] = "India"
        return ", ".join(address_text(value[key]) for key in
                         ("name", "addressLocality", "addressRegion", "addressCountry") if key in value)
    return str(value or "")


def structured_details(documents):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if "JobPosting" in ([kind] if isinstance(kind, str) else kind):
                yield value
            for item in value.values():
                if isinstance(item, (dict, list)):
                    yield from walk(item)
    for raw in documents:
        try:
            postings = list(walk(json.loads(raw)))
        except (ValueError, TypeError):
            continue
        if len(postings) == 1:
            posting = postings[0]
            # Applicant countries restrict remote roles; don't infer worldwide from TELECOMMUTE.
            locations = posting.get("applicantLocationRequirements") or posting.get("jobLocation", [])
            return plain_text(posting.get("description")), address_text(locations)
    return "", ""


DETAIL_SCRIPT = """() => {
 const text = selectors => {
   for (const s of selectors) {
     const e = document.querySelector(s);
     if (e && e.innerText.trim()) return e.innerText;
   }
   return '';
 };
 return {
   documents: [...document.querySelectorAll('script[type="application/ld+json"]')].map(e=>e.textContent),
   description: text(['[itemprop="description"]', '[data-automation-id="jobPostingDescription"]',
     '.job-description', '.jobDescription', '#job-description', '#jobDescription',
     '.job-description-content', '[data-testid="job-description"]', '.posting-page .content',
     '#content .content', '.job-details-description', '#description',
     '.job-description-container', '.job-detail .description', '.job .description']),
   location: text(['[itemprop="jobLocation"]', '[data-automation-id="locations"]',
     '[data-automation-id="location"]', '.job-location', '.jobLocation', '.location'])
 };
}"""


async def read_details(browser, job):
    if job.description:
        return job
    context = await browser.new_context()
    try:
        page = await context.new_page()
        response = await page.goto(job.url, wait_until="domcontentloaded", timeout=25000)
        if not response or response.status >= 400:
            raise RuntimeError(f"detail page HTTP {response.status if response else 'none'}")
        for attempt in range(5):
            try:
                payload = await page.evaluate(DETAIL_SCRIPT)
            except Exception as exc:
                if attempt == 4 or not any(message in str(exc).lower() for message in
                                          ("execution context was destroyed", "cannot find context")):
                    raise
                await page.wait_for_timeout(1000)
                continue
            description, location = structured_details(payload["documents"])
            description = description or payload["description"]
            location = location or payload["location"]
            if description:
                return replace(job, description=description, location=location)
            await page.wait_for_timeout(1000)
        raise RuntimeError("no isolated job description found; retry next run")
    finally:
        await context.close()



async def filter_changes(browser, candidates, updated, logger, reports=None):
    import asyncio
    semaphore = asyncio.Semaphore(4)

    if reports is None:
        reports = {}
    for company, jobs in candidates.items():
        reports[company] = {"candidates": len(jobs), "matched": 0, "skipped": 0, "pending": 0}

    async def check(company, job):
        async with semaphore:
            try:
                detail = await read_details(browser, job)
                words = matched_keywords(detail.description)
                region = geography(detail.location, detail.description)
                if words and region:
                    reports[company]["matched"] += 1
                    return company, replace(detail, matched=words, eligibility=region)
                reports[company]["skipped"] += 1
                logger.info("%s: skipped %s (location=%r, keyword matches=%s)",
                            company, job.url, detail.location, ", ".join(words) or "none")
            except Exception as exc:
                # Failed detail reads stay unseen so a later run can retry.
                reports[company]["pending"] += 1
                updated[company].pop(job.url, None)
                logger.warning("%s: details pending for %s: %s", company, job.url, exc)
            return company, None

    checked = await asyncio.gather(*(check(company, job) for company, jobs in candidates.items() for job in jobs))
    alerts = {}
    for company, job in checked:
        if job:
            alerts.setdefault(company, []).append(job)
    logger.info("Filter: %d newly detected jobs; %d eligible for email",
                sum(map(len, candidates.values())), sum(map(len, alerts.values())))
    return alerts
