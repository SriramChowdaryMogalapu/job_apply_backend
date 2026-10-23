import asyncio
import os
import re
import csv
import time
from typing import List, Dict, Tuple, Any, Callable, Optional
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from sentence_transformers import SentenceTransformer, util
from db import init_db_pool, close_db_pool, sync_jobs_pooled

# ----------------- CONFIGURATION -----------------
TARGET_LOCATIONS = {"bengaluru", "bangalore", "hyderabad", "india"}
EXCLUDE_REGIONS = {
    "north america", "us remote", "usa remote", "europe", 
    "uk remote", "emea", "apac", "london", "singapore", "latam", "canada"
}

KEYWORDS = {
    "software", "engineer", "sde", "backend", "fullstack", "developer",
    "site reliability", "associate software engineer", "software development engineer",
    "sre", "backend engineer", "full stack developer", "backend developer",
    "frontend developer", "frontend engineer", "react developer", "python developer"
}

DISQUALIFIED_TITLE_KEYWORDS = {
    "lead", "manager", "director", "staff", "principal", "sde 3", "sde-3", 
    "sde iii", "architect", "head of", "vp", "senior manager", "senior", "data", "vice president",
    "intern", "internship"
}

CANDIDATE_SKILLS = {
    "python", "javascript", "typescript", "react", "next.js", "flask", 
    "fastapi", "postgresql", "mongodb", "aws", "docker", "rag", "sql",
    "java", "c++", "node.js", "express.js", "redis", "graphql", "tailwind"
}

USER_DATA_DIR = os.path.expanduser("~/.naukri_browser_profile")
CSV_OUTPUT_PATH = "jobs_extracted.csv"

STEALTH_EVASION_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
"""

MY_RESUME_PROFILE = """
Candidate: Sriram Chowdary Mogalapu
Current Role: Software Development Engineer-1 at Piramal Finance (Jul 2025-Present)
Prior Role: BIU Intern at Piramal Finance (Jan 2025-Jul 2025)
Education: B.Tech in CSE from NIT Raipur (2021-2025), CPI: 9.14
Experience Bracket: SDE-1 / 0-2 Years Experience
Key Skills: Python, JavaScript, TypeScript, React, Next.js, Flask, FastAPI, PostgreSQL, MongoDB, AWS, Docker, RAG
"""

# Initialize local embedding model once for zero-cost ranking
LOCAL_EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
RESUME_EMBEDDING = LOCAL_EMBEDDER.encode(MY_RESUME_PROFILE, convert_to_tensor=True)

# ----------------- REGEX PATTERNS FOR FIELD EXTRACTION -----------------

EXP_PATTERN = re.compile(r"(\d+)\s*(?:-|to)?\s*(\d+)?\s*(?:years?|yrs?)", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_PATTERN = re.compile(r"(?:\+91[\-\s]?)?[6-9]\d{9}\b")

# Matches patterns like "apply by 31-12-2026", "deadline: Oct 30", "closing date: 15/11/2026"
DEADLINE_PATTERN = re.compile(
    r"(?:apply\s+before|apply\s+by|deadline|closing\s+date|valid\s+till|last\s+date)[\s:]*([A-Za-z0-9\s,\/\-\.]{4,20})",
    re.IGNORECASE
)

# Matches relative dates in text (e.g. "posted 2 days ago", "1 day ago")
POSTED_PATTERN = re.compile(r"(\d+\+?\s*(?:day|hour|week|month)s?\s*ago|just\s*posted|today|yesterday)", re.IGNORECASE)


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text)


def is_target_location(loc_str: str) -> bool:
    loc_l = loc_str.lower()
    if any(loc in loc_l for loc in TARGET_LOCATIONS):
        return True
    if "remote" in loc_l or "work from home" in loc_l:
        return not any(ex in loc_l for ex in EXCLUDE_REGIONS)
    return False


def extract_required_experience(text: str) -> Tuple[int, int, str]:
    matches = EXP_PATTERN.findall(text)
    if not matches:
        return 0, 99, "Not specified"
    min_exp, max_exp = 99, 0
    for match in matches:
        low = int(match[0])
        high = int(match[1]) if match[1] else low
        if low < min_exp:
            min_exp = low
        if high > max_exp:
            max_exp = high

    exp_str = f"{min_exp} - {max_exp} yrs" if max_exp > min_exp else f"{min_exp}+ yrs"
    return (min_exp if min_exp != 99 else 0), max_exp, exp_str


def extract_contacts(text: str) -> str:
    emails = EMAIL_PATTERN.findall(text)
    phones = PHONE_PATTERN.findall(text)
    # Deduplicate while preserving order
    unique_contacts = list(dict.fromkeys(emails + phones))
    return ", ".join(unique_contacts) if unique_contacts else "Not specified"


def extract_deadline(text: str) -> str:
    match = DEADLINE_PATTERN.search(text)
    if match:
        raw_val = match.group(1).strip()
        # Clean trailing punctuation
        return raw_val.strip(".,;:|")
    return "Not specified"


def extract_skills_from_text(text: str, fallback_tags: Optional[List[str]] = None) -> str:
    found_skills = set(fallback_tags or [])
    text_l = text.lower()
    for skill in CANDIDATE_SKILLS:
        if re.search(rf"\b{re.escape(skill)}\b", text_l):
            found_skills.add(skill.title())
    return ", ".join(sorted(found_skills)) if found_skills else "General SDE"


async def with_retry(func: Callable, *args, retries: int = 2, delay: float = 2.0, **kwargs) -> Any:
    for attempt in range(1, retries + 2):
        try:
            return await func(*args, **kwargs)
        except Exception as err:
            if attempt <= retries:
                await asyncio.sleep(delay)
            else:
                raise err


# ----------------- 1. ATS FETCHERS -----------------

async def _fetch_greenhouse_raw(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_token}/jobs?content=true"
    res = await client_http.get(url)
    res.raise_for_status()
    raw = res.json().get("jobs", [])
    jobs = []
    for j in raw:
        content = clean_html(j.get("content", ""))
        jobs.append({
            "id": f"gh_{j.get('id')}",
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", "").strip(),
            "url": j.get("absolute_url", ""),
            "description": content[:2500],
            "posted_date": j.get("updated_at", "")[:10] or "Not specified",
            "company": company_token.title(),
            "source": "greenhouse",
            "raw_tags": []
        })
    return jobs

async def fetch_greenhouse(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    try:
        return await with_retry(_fetch_greenhouse_raw, company_token, client_http)
    except Exception:
        return []

async def _fetch_lever_raw(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    url = f"https://api.lever.co/v0/postings/{company_token}?mode=json"
    res = await client_http.get(url)
    res.raise_for_status()
    raw = res.json()
    if not isinstance(raw, list):
        return []

    jobs = []
    for j in raw:
        if not isinstance(j, dict):
            continue
        cat = j.get("categories") or {}
        loc = cat.get("location", "") if isinstance(cat, dict) else ""
        desc = j.get("descriptionPlain", "") or clean_html(j.get("description", ""))
        
        # Lever returns createdAt timestamp in milliseconds
        created_at_ms = j.get("createdAt")
        posted_str = time.strftime("%Y-%m-%d", time.gmtime(created_at_ms / 1000.0)) if created_at_ms else "Not specified"

        jobs.append({
            "id": f"lev_{j.get('id')}",
            "title": (j.get("text") or "").strip(),
            "location": f"{loc} {j.get('workplaceType', '')}".strip(),
            "url": j.get("hostedUrl", ""),
            "description": desc[:2500],
            "posted_date": posted_str,
            "company": company_token.title(),
            "source": "lever",
            "raw_tags": []
        })
    return jobs

async def fetch_lever(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    try:
        return await with_retry(_fetch_lever_raw, company_token, client_http)
    except Exception:
        return []

async def _fetch_ashby_raw(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company_token}"
    res = await client_http.get(url)
    res.raise_for_status()
    raw = res.json().get("jobs", [])
    jobs = []
    for j in raw:
        desc = j.get("descriptionPlain", "") or clean_html(j.get("descriptionHtml", ""))
        jobs.append({
            "id": f"ash_{j.get('id')}",
            "title": j.get("title", "").strip(),
            "location": f"{j.get('location', '')} {'Remote' if j.get('isRemote') else ''}".strip(),
            "url": j.get("jobUrl", ""),
            "description": desc[:2500],
            "posted_date": (j.get("publishedAt") or "")[:10] or "Not specified",
            "company": company_token.title(),
            "source": "ashby",
            "raw_tags": []
        })
    return jobs

async def fetch_ashby(company_token: str, client_http: httpx.AsyncClient) -> List[Dict]:
    try:
        return await with_retry(_fetch_ashby_raw, company_token, client_http)
    except Exception:
        return []

# ----------------- 2. AGGREGATORS & SOCIAL -----------------

async def _fetch_instahyre_raw(skill: str, client_http: httpx.AsyncClient, limit: int = 15) -> List[Dict]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    url = f"https://www.instahyre.com/api/v1/job_search/?skills={skill}&limit={limit}"
    res = await client_http.get(url, headers=headers)
    res.raise_for_status()
    raw = res.json().get("objects", [])
    jobs = []
    for item in raw:
        employer = item.get("employer", {}) or {}
        locs = item.get("locations", [])
        loc_str = ", ".join(locs) if isinstance(locs, list) else str(locs)
        raw_url = item.get("public_url", "")
        full_url = raw_url if raw_url.startswith("http") else f"https://www.instahyre.com{raw_url}"
        keywords = item.get("keywords", [])

        jobs.append({
            "id": f"instahyre_{item.get('id')}",
            "title": item.get("title", "").strip(),
            "company": employer.get("company_name", "Unknown"),
            "location": loc_str or "India",
            "url": full_url,
            "description": f"Keywords and Tech Stack: {', '.join(keywords)}",
            "posted_date": "Recently active",
            "source": "instahyre",
            "raw_tags": keywords
        })
    return jobs

async def fetch_instahyre(skill: str, client_http: httpx.AsyncClient, limit: int = 15) -> List[Dict]:
    try:
        return await with_retry(_fetch_instahyre_raw, skill, client_http, limit=limit)
    except Exception:
        return []

async def _fetch_linkedin_raw(keywords: str, location: str, client_http: httpx.AsyncClient, limit: int = 15) -> List[Dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    params = {"keywords": keywords, "location": location, "start": 0, "f_TPR": "r86400"}
    res = await client_http.get(url, headers=headers, params=params)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    jobs = []
    for card in soup.find_all("li"):
        t_tag = card.find("h3", class_="base-search-card__title")
        c_tag = card.find("h4", class_="base-search-card__subtitle")
        l_tag = card.find("span", class_="job-search-card__location")
        link_tag = card.find("a", class_="base-card__full-link")
        time_tag = card.find("time")

        if not (t_tag and link_tag):
            continue

        clean_url = link_tag.get("href", "").split("?")[0]
        posted_val = time_tag.get("datetime") or time_tag.get_text(strip=True) if time_tag else "Past 24 hours"

        jobs.append({
            "id": f"linkedin_{clean_url.split('-')[-1]}",
            "title": t_tag.get_text(strip=True),
            "company": c_tag.get_text(strip=True) if c_tag else "Unknown",
            "location": l_tag.get_text(strip=True) if l_tag else location,
            "url": clean_url,
            "description": f"Role: {t_tag.get_text(strip=True)} at {c_tag.get_text(strip=True) if c_tag else 'Unknown'}",
            "posted_date": posted_val,
            "source": "linkedin",
            "raw_tags": []
        })
        if len(jobs) >= limit:
            break
    return jobs

async def fetch_linkedin(keywords: str, location: str, client_http: httpx.AsyncClient, limit: int = 15) -> List[Dict]:
    try:
        return await with_retry(_fetch_linkedin_raw, keywords, location, client_http, limit=limit)
    except Exception:
        return []

# ----------------- 3. BROWSER FETCHERS (NAUKRI & INDEED) -----------------

async def fetch_naukri(keyword: str = "backend developer", location: str = "bangalore", limit: int = 10) -> List[Dict]:
    jobs = []
    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=True,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script("delete Object.getPrototypeOf(navigator).webdriver;")

            naukri_url = f"https://www.naukri.com/{keyword.lower().replace(' ', '-')}-jobs-in-{location.lower().replace(' ', '-')}"
            await page.goto(naukri_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_selector("div.srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple", timeout=12000)
            cards = await page.query_selector_all("div.srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple")

            for card in cards[:limit]:
                t_elem = await card.query_selector("a.title, a[class*='title']")
                c_elem = await card.query_selector("a.comp-name, [class*='comp-name']")
                l_elem = await card.query_selector(".loc-wrap span, [class*='locWdt'], [class*='location']")
                exp_elem = await card.query_selector(".exp-wrap span, [class*='expwdth'], [class*='experience']")
                post_elem = await card.query_selector(".job-post-day, [class*='job-tuple-footer'] span")

                title = (await t_elem.inner_text()).strip() if t_elem else "N/A"
                link = (await t_elem.get_attribute("href")) if t_elem else ""
                company = (await c_elem.inner_text()).strip() if c_elem else "Unknown"
                loc = (await l_elem.inner_text()).strip() if l_elem else location
                exp = (await exp_elem.inner_text()).strip() if exp_elem else ""
                posted = (await post_elem.inner_text()).strip() if post_elem else "Recently"

                if link and not link.startswith("http"):
                    link = f"https://www.naukri.com{link}"

                if title != "N/A" and link:
                    jobs.append({
                        "id": f"naukri_{hash(link)}",
                        "title": title,
                        "company": company,
                        "location": loc,
                        "url": link,
                        "description": f"Role: {title} at {company}. Experience required: {exp}.",
                        "posted_date": posted,
                        "source": "naukri",
                        "raw_tags": []
                    })
            await context.close()
        except Exception as e:
            print(f"    [!] Naukri extraction note: {e}")
    return jobs

async def fetch_indeed(query: str = "SDE", location: str = "Bengaluru", limit: int = 15) -> List[Dict]:
    jobs = []
    query_param = query.replace(" ", "+")
    location_param = location.replace(" ", "+")
    url = f"https://in.indeed.com/jobs?q={query_param}&l={location_param}&sort=date"

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768}
            )
            page = await context.new_page()
            await page.add_init_script(STEALTH_EVASION_SCRIPT)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector(".job_seen_beacon, td.resultContent", timeout=18000)

            cards = await page.query_selector_all(".job_seen_beacon, td.resultContent")

            for card in cards[:limit]:
                anchor = await card.query_selector("a.jcs-JobTitle, h2.jobTitle a, a[data-jk]")
                company_elem = await card.query_selector("[data-testid='company-name'], span.companyName")
                loc_elem = await card.query_selector("[data-testid='text-location'], div.companyLocation")
                date_elem = await card.query_selector("[data-testid='myJobsStateDate'], span.date")

                title = (await anchor.inner_text()).strip() if anchor else "N/A"
                company = (await company_elem.inner_text()).strip() if company_elem else "Unknown"
                loc = (await loc_elem.inner_text()).strip() if loc_elem else location
                posted = (await date_elem.inner_text()).strip() if date_elem else "Recently"

                href = await anchor.get_attribute("href") if anchor else ""
                jk = await anchor.get_attribute("data-jk") if anchor else None

                if jk:
                    full_url = f"https://in.indeed.com/viewjob?jk={jk}"
                elif href.startswith("/"):
                    full_url = f"https://in.indeed.com{href}"
                else:
                    full_url = href

                if title != "N/A" and full_url:
                    jobs.append({
                        "id": f"indeed_{jk or hash(full_url)}",
                        "title": title,
                        "company": company,
                        "location": loc,
                        "url": full_url,
                        "description": f"Indeed Job Posting for {title} at {company}",
                        "posted_date": posted,
                        "source": "indeed",
                        "raw_tags": []
                    })
            await browser.close()
        except Exception as e:
            print(f"    [!] Indeed extraction note: {e}")
    return jobs

# ----------------- 4. LOCAL ENRICHMENT & RANKING -----------------

def enrich_and_rank_jobs(jobs: List[Dict]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    candidates = []

    for job in jobs:
        desc = job.get("description", "")
        title = job.get("title", "")
        combined_text = f"{title} {desc}"

        # Extract Experience
        min_yoe, max_yoe, exp_display = extract_required_experience(combined_text)
        job["experience_required"] = exp_display

        # Drop jobs strictly requiring 4+ YOE (SDE-1 safety)
        if min_yoe >= 4:
            continue

        # Drop management titles
        if any(bad in title.lower() for bad in DISQUALIFIED_TITLE_KEYWORDS):
            continue

        # Must have relevant SDE keyword
        if not any(k in title.lower() for k in KEYWORDS):
            continue

        # Extract Contacts, Deadline, and Skills
        job["contacts"] = extract_contacts(combined_text)
        job["last_date_to_apply"] = extract_deadline(combined_text)
        job["skills_required"] = extract_skills_from_text(combined_text, job.get("raw_tags", []))

        candidates.append(job)

    if not candidates:
        return [], [], jobs

    # Local sentence-transformer batch ranking
    job_texts = [f"{j['title']} {j['company']} {j.get('description', '')[:800]}" for j in candidates]
    job_embs = LOCAL_EMBEDDER.encode(job_texts, convert_to_tensor=True, batch_size=64)
    cos_sims = util.cos_sim(RESUME_EMBEDDING, job_embs)[0].tolist()

    preferred = []
    suggested = []

    for idx, sim in enumerate(cos_sims):
        score = int(sim * 100)
        candidates[idx]["fit_score"] = score

        if is_target_location(candidates[idx].get("location", "")) and score >= 45:
            candidates[idx]["category"] = "Preferred"
            preferred.append(candidates[idx])
        else:
            candidates[idx]["category"] = "Suggested"
            suggested.append(candidates[idx])

    preferred.sort(key=lambda x: x["fit_score"], reverse=True)
    suggested.sort(key=lambda x: x["fit_score"], reverse=True)

    return preferred, suggested, jobs

# ----------------- 5. CSV EXPORT -----------------

def export_to_csv(jobs: List[Dict], filename: str = CSV_OUTPUT_PATH):
    fieldnames = [
        "title",
        "company",
        "location",
        "category",
        "fit_score",
        "experience_required",
        "skills_required",
        "posted_date",
        "last_date_to_apply",
        "contacts",
        "source",
        "url"
    ]

    with open(filename, mode="w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            writer.writerow({
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "category": job.get("category", "General"),
                "fit_score": f"{job.get('fit_score', 0)}%",
                "experience_required": job.get("experience_required", "Not specified"),
                "skills_required": job.get("skills_required", ""),
                "posted_date": job.get("posted_date", "Not specified"),
                "last_date_to_apply": job.get("last_date_to_apply", "Not specified"),
                "contacts": job.get("contacts", "Not specified"),
                "source": job.get("source", "").upper(),
                "url": job.get("url", "")
            })

# ----------------- MAIN PIPELINE -----------------

async def main():
    start_time = time.time()
    print("\n" + "="*70)
    print(" INITIATING FAST LOCAL MULTI-SOURCE HARVEST (REGEX EXTRACTION & CSV)")
    print("="*70)

    stats: Dict[str, Dict[str, Any]] = {}
    all_jobs: List[Dict] = []

    # Phase 1: Fast Async HTTP
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client_http:
        for comp in ["groww", "airbnb", "cloudflare"]:
            key = f"Greenhouse ({comp})"
            jobs = await fetch_greenhouse(comp, client_http)
            stats[key] = {"fetched": len(jobs), "status": "Success" if jobs else "0 jobs"}
            all_jobs.extend(jobs)

        for comp in ["spotify"]:
            key = f"Lever ({comp})"
            jobs = await fetch_lever(comp, client_http)
            stats[key] = {"fetched": len(jobs), "status": "Success" if jobs else "0 jobs"}
            all_jobs.extend(jobs)

        for comp in ["linear", "replit"]:
            key = f"Ashby ({comp})"
            jobs = await fetch_ashby(comp, client_http)
            stats[key] = {"fetched": len(jobs), "status": "Success" if jobs else "0 jobs"}
            all_jobs.extend(jobs)

        for skill in ["Backend", "Fullstack"]:
            key = f"Instahyre ({skill})"
            jobs = await fetch_instahyre(skill, client_http, limit=15)
            stats[key] = {"fetched": len(jobs), "status": "Success" if jobs else "0 jobs"}
            all_jobs.extend(jobs)

        key = "LinkedIn Guest"
        jobs = await fetch_linkedin("Backend Developer", "Bengaluru", client_http, limit=10)
        stats[key] = {"fetched": len(jobs), "status": "Success" if jobs else "0 jobs"}
        all_jobs.extend(jobs)

    # Phase 2: Independent Browser Extractions
    naukri_jobs = await fetch_naukri(keyword="backend developer", location="bangalore", limit=8)
    stats["Naukri (Browser)"] = {"fetched": len(naukri_jobs), "status": "Success" if naukri_jobs else "0 jobs"}
    all_jobs.extend(naukri_jobs)

    indeed_jobs = await fetch_indeed(query="SDE", location="Bengaluru", limit=12)
    stats["Indeed (Browser)"] = {"fetched": len(indeed_jobs), "status": "Success" if indeed_jobs else "0 jobs"}
    all_jobs.extend(indeed_jobs)

    # Print Itemized Fetch Breakdown Table
    print("\n" + "-"*55)
    print(f"{'SOURCE / TARGET':<30} | {'FETCHED':<8} | {'STATUS'}")
    print("-"*55)
    total_raw = 0
    for src, data in stats.items():
        print(f"{src:<30} | {data['fetched']:<8} | {data['status']}")
        total_raw += data['fetched']
    print("-"*55)
    print(f"{'TOTAL RAW FETCHED':<30} | {total_raw:<8} |")
    print("-"*55 + "\n")

    # Phase 3: Deduplication
    unique_jobs = []
    seen = set()
    for j in all_jobs:
        u = j.get("url", "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        unique_jobs.append(j)

    print(f"[*] Total Unique Listings: {len(unique_jobs)}")
    print("[*] Parsing experience, deadlines, contacts, skills, and ranking locally...")

    preferred_jobs, suggested_jobs, all_enriched = enrich_and_rank_jobs(unique_jobs)

    # Export to CSV
    combined_export = preferred_jobs + suggested_jobs
    export_to_csv(combined_export, CSV_OUTPUT_PATH)

    elapsed = time.time() - start_time
    print(f"[*] Done in {elapsed:.2f} seconds. Exported {len(combined_export)} jobs to '{CSV_OUTPUT_PATH}'.")

    # ----------------- PREVIEW PREFERRED JOBS -----------------
    print("\n" + "="*80)
    print(f" PREFERRED JOBS (TARGET HUBS + SDE-1 MATCH) [{len(preferred_jobs)}]")
    print("="*80 + "\n")

    for j in preferred_jobs[:10]:
        print(f"[{j['fit_score']}% FIT] [{j['source'].upper()}] {j['company']} — {j['title']}")
        print(f"  Location: {j['location']} | Exp: {j.get('experience_required')}")
        print(f"  Skills: {j.get('skills_required')}")
        print(f"  Posted: {j.get('posted_date')} | Apply By: {j.get('last_date_to_apply')}")
        print(f"  Contacts: {j.get('contacts')}")
        print(f"  URL: {j['url']}\n")

    preferred_jobs, suggested_jobs, _ = enrich_and_rank_jobs(unique_jobs)
    combined_jobs = preferred_jobs + suggested_jobs

    # 1. Export CSV backup
    export_to_csv(combined_jobs, CSV_OUTPUT_PATH)

    # 2. Sync to Supabase via Pooled Connection
    try:
        await init_db_pool()
        total_synced = await sync_jobs_pooled(combined_jobs)
        print(f"[*] Pooled DB Sync Complete: Synced {total_synced} listings to Supabase.")
    except Exception as e:
        print(f"[!] Database sync error: {e}")
    finally:
        await close_db_pool()

if __name__ == "__main__":
    asyncio.run(main())