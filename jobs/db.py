import os
import hashlib
from typing import List, Dict, Tuple
import asyncpg

# Set via environment variable: export DATABASE_URL="your_pooled_uri_here"
DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: asyncpg.Pool = None

async def init_db_pool(uri: str = DATABASE_URL) -> asyncpg.Pool:
    """Initializes a connection pool using the provided Supabase pooled URI."""
    global _pool
    if not uri:
        raise ValueError("DATABASE_URL is not set. Provide your Supabase pooled connection string.")
    
    # Strip SQLAlchemy dialect prefix if present
    clean_uri = uri.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgres://")

    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=clean_uri,
            min_size=2,
            max_size=10,
            statement_cache_size=0  # Required for Supabase PgBouncer (Transaction mode)
        )
    return _pool

async def close_db_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

def generate_dedup_id(job: Dict) -> str:
    """Generates a stable unique hash across scraping runs."""
    company = job.get("company", "").strip().lower()
    title = job.get("title", "").strip().lower()
    location = job.get("location", "").strip().lower()
    source = job.get("source", "").strip().lower()
    
    canonical_key = f"{company}_{title}_{location}_{source}"
    return hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:20]

async def sync_jobs_pooled(jobs: List[Dict]) -> Tuple[int, int]:
    """
    Upserts jobs in batch using the connection pool.
    - Preserves user-assigned status ('QUEUED', 'APPLIED', 'IGNORED').
    - Preserves 'notified' flag.
    - Updates 'last_seen_at' and latest fit score.
    Returns: (total_processed, newly_inserted)
    """
    if not jobs:
        return 0, 0

    pool = await init_db_pool()

    # Deduplicate in-memory by generated ID before hitting the DB
    deduped_jobs: Dict[str, Dict] = {}
    for j in jobs:
        dedup_id = generate_dedup_id(j)
        deduped_jobs[dedup_id] = j

    total_processed = len(deduped_jobs)

    # Deconstruct into parallel arrays for unnest
    ids = []
    urls = []
    titles = []
    companies = []
    locations = []
    categories = []
    fit_scores = []
    exp_required = []
    skills_required = []
    posted_dates = []
    last_dates = []
    contacts = []
    sources = []

    for jid, j in deduped_jobs.items():
        ids.append(jid)
        urls.append(j.get("url", ""))
        titles.append(j.get("title", ""))
        companies.append(j.get("company", ""))
        locations.append(j.get("location", ""))
        categories.append(j.get("category", "General"))
        fit_scores.append(int(j.get("fit_score", 0)))
        exp_required.append(j.get("experience_required", "Not specified"))
        skills_required.append(j.get("skills_required", ""))
        posted_dates.append(j.get("posted_date", "Not specified"))
        last_dates.append(j.get("last_date_to_apply", "Not specified"))
        contacts.append(j.get("contacts", "Not specified"))
        sources.append(j.get("source", "").upper())

    # Single-query batch upsert with unnest()
    upsert_query = """
    INSERT INTO public.jobs (
        id, url, title, company, location, category, fit_score,
        experience_required, skills_required, posted_date, 
        last_date_to_apply, contacts, source, status, notified, 
        first_seen_at, last_seen_at
    )
    SELECT
        u.id, u.url, u.title, u.company, u.location, u.category, u.fit_score,
        u.exp, u.skills, u.posted, u.last_date, u.contacts, u.source,
        'DISCOVERED', FALSE, NOW(), NOW()
    FROM UNNEST(
        $1::text[], $2::text[], $3::text[], $4::text[], $5::text[],
        $6::text[], $7::int[],  $8::text[], $9::text[], $10::text[],
        $11::text[], $12::text[], $13::text[]
    ) AS u(
        id, url, title, company, location, category, fit_score,
        exp, skills, posted, last_date, contacts, source
    )
    ON CONFLICT (id) DO UPDATE SET
        fit_score = EXCLUDED.fit_score,
        last_seen_at = NOW()
    RETURNING (xmax = 0) AS is_new;
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                upsert_query,
                ids, urls, titles, companies, locations, categories,
                fit_scores, exp_required, skills_required, posted_dates,
                last_dates, contacts, sources
            )

    # In PostgreSQL: xmax = 0 evaluates to True when an INSERT occurred rather than an UPDATE
    newly_inserted = sum(1 for r in rows if r["is_new"])
    refreshed_existing = total_processed - newly_inserted
    
    print(f"[*] Processed {total_processed} jobs: {newly_inserted} new, {refreshed_existing} refreshed.")

    return total_processed, newly_inserted

async def get_unnotified_preferred_jobs(limit: int = 10) -> List[Dict]:
    """Fetches high-priority jobs that have not yet been sent to your morning digest."""
    pool = await init_db_pool()
    query = """
    SELECT id, title, company, location, fit_score, skills_required, url, source
    FROM public.jobs
    WHERE status = 'DISCOVERED' 
      AND notified = FALSE 
      AND category = 'Preferred'
    ORDER BY fit_score DESC
    LIMIT $1;
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, limit)
        return [dict(row) for row in rows]

async def mark_jobs_as_notified(job_ids: List[str]):
    """Marks delivered digest jobs as notified so they do not trigger duplicates."""
    if not job_ids:
        return
    pool = await init_db_pool()
    query = "UPDATE public.jobs SET notified = TRUE WHERE id = ANY($1::text[]);"
    async with pool.acquire() as conn:
        await conn.execute(query, job_ids)