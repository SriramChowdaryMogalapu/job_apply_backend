import os
import re
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import asyncpg

# Environment config
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres.faeevhssnyjunbsguqce:%24TxRe%23J_4PG%262%21L@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres")
CLEAN_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("postgres+asyncpg://", "postgres://")

app = FastAPI(title="JobApply Backend & Autofill Copilot API")

# Enable CORS for local Next.js frontend and Chrome Extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your domain/extension ID in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool: Optional[asyncpg.Pool] = None

@app.on_event("startup")
async def startup():
    global _pool
    if not CLEAN_DB_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing.")
    _pool = await asyncpg.create_pool(
        dsn=CLEAN_DB_URL,
        min_size=2,
        max_size=10,
        statement_cache_size=0,
        ssl="require"
    )

@app.on_event("shutdown")
async def shutdown():
    global _pool
    if _pool:
        await _pool.close()

# ----------------- PYDANTIC SCHEMAS -----------------

class UserProfileSchema(BaseModel):
    first_name: str
    middle_name: Optional[str] = ""
    last_name: str
    full_name: str
    phone_code: str = "+91"
    phone: str
    email: str
    address_line1: Optional[str] = ""
    city: Optional[str] = "Bengaluru"
    state: Optional[str] = "Karnataka"
    postal_code: Optional[str] = ""
    country: Optional[str] = "India"
    current_company: Optional[str] = ""
    current_role: Optional[str] = ""
    linkedin_url: Optional[str] = ""
    github_url: Optional[str] = ""
    portfolio_url: Optional[str] = ""
    notice_period_days: Optional[int] = 30
    current_ctc: Optional[str] = ""
    expected_ctc: Optional[str] = ""
    years_of_experience: Optional[float] = 1.0

class LearnFieldSchema(BaseModel):
    raw_question: str
    answer: str
    field_type: Optional[str] = "text"

# ----------------- 1. USER PROFILE ENDPOINTS -----------------

@app.get("/api/profile", response_model=UserProfileSchema)
async def get_profile():
    """Returns the primary user profile for frontend & autofill extension."""
    query = "SELECT * FROM public.user_profile WHERE id = 'primary_profile';"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(query)
        if not row:
            raise HTTPException(status_code=404, detail="Profile not configured.")
        return dict(row)

@app.put("/api/profile", response_model=UserProfileSchema)
async def update_profile(payload: UserProfileSchema):
    """Updates user personal and employment details."""
    query = """
    INSERT INTO public.user_profile (
        id, first_name, middle_name, last_name, full_name,
        phone_code, phone, email, address_line1, city, state, postal_code, country,
        current_company, current_role, linkedin_url, github_url, portfolio_url,
        notice_period_days, current_ctc, expected_ctc, years_of_experience, updated_at
    ) VALUES (
        'primary_profile', $1, $2, $3, $4,
        $5, $6, $7, $8, $9, $10, $11, $12,
        $13, $14, $15, $16, $17,
        $18, $19, $20, $21, NOW()
    )
    ON CONFLICT (id) DO UPDATE SET
        first_name = EXCLUDED.first_name,
        middle_name = EXCLUDED.middle_name,
        last_name = EXCLUDED.last_name,
        full_name = EXCLUDED.full_name,
        phone_code = EXCLUDED.phone_code,
        phone = EXCLUDED.phone,
        email = EXCLUDED.email,
        address_line1 = EXCLUDED.address_line1,
        city = EXCLUDED.city,
        state = EXCLUDED.state,
        postal_code = EXCLUDED.postal_code,
        country = EXCLUDED.country,
        current_company = EXCLUDED.current_company,
        current_role = EXCLUDED.current_role,
        linkedin_url = EXCLUDED.linkedin_url,
        github_url = EXCLUDED.github_url,
        portfolio_url = EXCLUDED.portfolio_url,
        notice_period_days = EXCLUDED.notice_period_days,
        current_ctc = EXCLUDED.current_ctc,
        expected_ctc = EXCLUDED.expected_ctc,
        years_of_experience = EXCLUDED.years_of_experience,
        updated_at = NOW()
    RETURNING *;
    """
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            query,
            payload.first_name, payload.middle_name, payload.last_name, payload.full_name,
            payload.phone_code, payload.phone, payload.email, payload.address_line1,
            payload.city, payload.state, payload.postal_code, payload.country,
            payload.current_company, payload.current_role, payload.linkedin_url,
            payload.github_url, payload.portfolio_url, payload.notice_period_days,
            payload.current_ctc, payload.expected_ctc, payload.years_of_experience
        )
        return dict(row)

# ----------------- 2. ADAPTIVE KNOWLEDGE BASE (LEARNING) -----------------

def normalize_key(text: str) -> str:
    """Normalizes noisy form labels (e.g. 'Are you authorized to work in India? *') -> 'authorized_to_work_in_india'"""
    text = re.sub(r"[^a-zA-Z0-9\s]", "", text.lower())
    words = text.strip().split()
    return "_".join(words[:6])  # keep first 6 significant words

@app.get("/api/autofill-payload")
async def get_autofill_payload():
    """Consolidated endpoint delivering profile + learned knowledge base to the Chrome Extension."""
    async with _pool.acquire() as conn:
        profile_row = await conn.fetchrow("SELECT * FROM public.user_profile WHERE id = 'primary_profile';")
        kb_rows = await conn.fetch("SELECT question_key, stored_answer, field_type FROM public.form_knowledge_base;")

    return {
        "profile": dict(profile_row) if profile_row else {},
        "knowledge_base": [dict(r) for r in kb_rows]
    }

@app.post("/api/knowledge-base/learn")
async def learn_field(payload: LearnFieldSchema):
    """Saves newly answered custom questions from the auto-filler."""
    norm_key = normalize_key(payload.raw_question)
    if not norm_key or not payload.answer.strip():
        return {"status": "skipped"}

    query = """
    INSERT INTO public.form_knowledge_base (question_key, stored_answer, field_type, frequency_used, last_updated)
    VALUES ($1, $2, $3, 1, NOW())
    ON CONFLICT (question_key) DO UPDATE SET
        stored_answer = EXCLUDED.stored_answer,
        frequency_used = public.form_knowledge_base.frequency_used + 1,
        last_updated = NOW();
    """
    async with _pool.acquire() as conn:
        await conn.execute(query, norm_key, payload.answer.strip(), payload.field_type)

    return {"status": "learned", "key": norm_key}

# ----------------- 3. JOB DETAILS & STATUS UPDATES -----------------

@app.get("/api/jobs/{job_id}")
async def get_job_by_id(job_id: str):
    """Used by the web frontend when opened via the Telegram deep link: /jobs/[job_id]"""
    query = "SELECT * FROM public.jobs WHERE id = $1;"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(query, job_id)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")
        return dict(row)

@app.patch("/api/jobs/{job_id}/status")
async def update_job_status(job_id: str, new_status: str):
    """Transitions job state (e.g. to 'APPLIED' or 'IGNORED')."""
    if new_status not in {"DISCOVERED", "QUEUED", "APPLIED", "IGNORED"}:
        raise HTTPException(status_code=400, detail="Invalid status.")
    
    query = "UPDATE public.jobs SET status = $1::job_status WHERE id = $2 RETURNING id, status;"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(query, new_status, job_id)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")
        return dict(row)