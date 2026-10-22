import os
import hashlib
from typing import List, Dict, Tuple
import asyncpg

# Set via environment variable: export DATABASE_URL="your_pooled_uri_here"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:%24TxRe%23J_4PG%262%21L@db.faeevhssnyjunbsguqce.supabase.co:5432/postgres")

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
    Returns: (total_inserted_or_refreshed, newly_inserted)
    """
    if not jobs:
        return 0, 0

    pool = await init_db_pool()

    # Prepare records for batch insertion
    records = []
    for j in jobs:
        records.append((
            generate_dedup_id(j),
            j.get("url", ""),
            j.get("title", ""),
            j.get("company", ""),
            j.get("location", ""),
            j.get("category", "General"),
            j.get("fit_score", 0),
            j.get("experience_required", "Not specified"),
            j.get("skills_required", ""),
            j.get("posted_date", "Not specified"),
            j.get("last_date_to_apply", "Not specified"),
            j.get("contacts", "Not specified"),
            j.get("source", "").upper()
        ))

    upsert_query = """
    INSERT INTO public.jobs (
        id, url, title, company, location, category, fit_score,
        experience_required, skills_required, posted_date, 
        last_date_to_apply, contacts, source, status, notified, 
        first_seen_at, last_seen_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7,
        $8, $9, $10,
        $11, $12, $13, 'DISCOVERED', FALSE,
        NOW(), NOW()
    )
    ON CONFLICT (id) DO UPDATE SET
        fit_score = EXCLUDED.fit_score,
        last_seen_at = NOW()
    RETURNING (xmax = 0) AS is_new;
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            results = await conn.executemany(upsert_query, records)

    # In PostgreSQL, xmax = 0 evaluates to True when an INSERT actually occurred rather than an UPDATE
    return len(records)

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