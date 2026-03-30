# VLCR — Vernacular Language Complaint Router

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![Vercel](https://img.shields.io/badge/frontend-Vercel-black)
![Railway](https://img.shields.io/badge/backend-Railway-purple)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

An AI-powered multilingual complaint routing system for Indian government services. Citizens file grievances in 12 Indian languages via web, WhatsApp, SMS, or IVR phone call. An 8-step pipeline (ingest → detect → translate → classify → route → dispatch → notify → track) handles every complaint automatically using Anthropic Claude for classification and Bhashini Dhruva for ASR and translation.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        VLCR Pipeline                        │
│                                                             │
│  Citizen Input                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │   Web    │  │WhatsApp  │  │   SMS    │  │IVR Call  │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       └─────────────┴──────────────┴──────────────┘        │
│                            │                                │
│              ┌─────────────▼─────────────┐                 │
│              │  1. Ingest (FastAPI)       │                 │
│              │  2. Language Detect        │ Bhashini        │
│              │  3. Translate → EN         │ LangID +        │
│              │  4. Translate → HI         │ Dhruva API      │
│              │  5. Classify (Claude AI)   │ Anthropic       │
│              │  6. Route (PostgreSQL)     │ SQLAlchemy      │
│              │  7. Dispatch (webhook/     │ async           │
│              │     email/cpgrams)         │                 │
│              │  8. Notify (SMS)           │ Gupshup/Twilio  │
│              └─────────────┬─────────────┘                 │
│                            │                                │
│              ┌─────────────▼─────────────┐                 │
│              │  PostgreSQL 15 + Redis 7   │                 │
│              │  (state + cache + dedup)   │                 │
│              └───────────────────────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

**Stack:** FastAPI + SQLAlchemy (async) + Alembic + PostgreSQL 15 + Redis 7 + Anthropic Claude + Bhashini Dhruva + Exotel IVR + Gupshup/Twilio SMS

---

## Repository Layout

```
vlcr/                          ← GitHub repo root
├── backend/                   ← Everything Python/FastAPI
│   ├── app/
│   │   ├── main.py
│   │   ├── core/              ← config, database, auth, redis, exceptions
│   │   ├── models/            ← SQLAlchemy ORM (6 tables)
│   │   ├── schemas/           ← Pydantic v2 request/response schemas
│   │   ├── services/          ← pipeline, classifier, nlp_service, notification
│   │   └── routers/           ← auth, complaints, tracking, dashboard,
│   │                             review, routing, pipeline, ivr
│   ├── alembic/               ← Migrations (env.py + versions/)
│   ├── tests/                 ← pytest + hypothesis property tests
│   ├── alembic.ini
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   └── vercel.json
├── frontend/
│   ├── index.html             ← Single-file HTML/JS frontend (no build step)
│   └── vercel.json
├── .gitignore
└── README.md
```

---

## Quick Start — Docker (Recommended)

```bash
git clone https://github.com/YOUR_USERNAME/vlcr.git
cd vlcr/backend

# 1. Configure environment
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY at minimum

# 2. Start all services (postgres + redis + backend)
docker-compose up --build -d

# 3. Seed demo departments and users
curl -X POST http://localhost:8000/api/v1/auth/seed-demo-dept

# 4. Open the frontend
open ../frontend/index.html
# or serve it: python -m http.server 3000 --directory ../frontend
```

Health check: `curl http://localhost:8000/api/health`

---

## Quick Start — Manual (Python)

```bash
cd vlcr/backend

# Create virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env: set DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY

# Run migrations
alembic upgrade head

# Start backend (with auto-reload)
uvicorn app.main:app --reload --port 8000

# Seed demo data
curl -X POST http://localhost:8000/api/v1/auth/seed-demo-dept

# Open frontend
open ../frontend/index.html
```

---

## Deploying Frontend to Vercel

1. Push this repo to GitHub
2. Go to [vercel.com](https://vercel.com) → **Add New Project** → import `vlcr`
3. In project settings:
   - **Root Directory:** `frontend`
   - **Framework Preset:** Other (Static)
   - **Build Command:** *(leave empty)*
   - **Output Directory:** `.`
4. Under **Environment Variables**, add:
   ```
   VLCR_API_URL = https://your-backend-on-railway.app
   ```
5. Click **Deploy**

Frontend will be live at `https://vlcr.vercel.app` (or your custom domain).

---

## Deploying Backend to Railway (Recommended)

Railway supports Docker, long-running processes, Celery workers, and persistent Redis — a better fit than Vercel for the FastAPI backend.

```bash
# 1. Connect GitHub repo on railway.app → New Project → Deploy from GitHub
#    Set Root Directory to: backend

# 2. Add plugins: PostgreSQL + Redis
#    Railway injects DATABASE_URL and REDIS_URL automatically.

# 3. Add all remaining env vars from .env.example in Railway dashboard

# 4. After first deploy, run migrations:
railway run alembic upgrade head

# 5. Seed demo departments:
railway run python -c "
import httpx, asyncio
asyncio.run(httpx.AsyncClient().post('https://your-app.railway.app/api/v1/auth/seed-demo-dept'))
"

# 6. Copy the Railway public URL → paste into Vercel's VLCR_API_URL env var
# 7. Update CORS_ORIGINS in Railway env vars to include your Vercel frontend URL
```

**Important:** Set `DEBUG=false` and a strong `SECRET_KEY` in Railway env vars before going live. The app will refuse to start in production with the default `SECRET_KEY`.

---

## If Backend Must Be on Vercel

> **Note:** Railway is strongly recommended for the backend. Vercel serverless has a 10-second function timeout and no persistent worker support (Celery won't work). Use these workarounds if Vercel is required:

- Replace Celery task calls with `FastAPI BackgroundTasks`
- Use **Upstash Redis** (HTTP-based, stateless-safe) instead of standard Redis
- Use **Vercel Postgres** or **Neon** for the database
- Run `alembic upgrade head` via Vercel CLI or CI before deploy (never at startup — `VERCEL=1` env var disables auto-migration)

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | **Yes (prod)** | *(default)* | JWT signing key — must change before production |
| `DATABASE_URL` | Yes | localhost | PostgreSQL connection string (`+asyncpg`) |
| `REDIS_URL` | No | localhost | Redis URL — degrades gracefully if unavailable |
| `ANTHROPIC_API_KEY` | No | — | Claude API key — uses mock classifier if absent |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-6` | Claude model string |
| `BHASHINI_API_KEY` | No | — | Bhashini ASR/translation — uses heuristic if absent |
| `BHASHINI_USER_ID` | No | — | Bhashini user ID |
| `EXOTEL_API_KEY` | No | — | Exotel IVR credentials |
| `SMS_PROVIDER` | No | `mock` | `mock` \| `gupshup` \| `twilio` |
| `CORS_ORIGINS` | No | localhost only | JSON array or comma-separated allowed origins |
| `DEBUG` | No | `false` | Enables SQL logging and disables prod guards |
| `VERCEL` | No | — | Set to `1` to skip auto-migration at startup |

---

## Demo Credentials

After running `seed-demo-dept`:

| Role | Username | Password |
|------|----------|----------|
| Super Admin | `admin` | `admin123` |
| Officer | `officer` | `officer123` |
| Reviewer | `reviewer` | `reviewer123` |

---

## API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/health` | — | Liveness probe |
| `POST` | `/api/v1/auth/login` | — | Get JWT token |
| `POST` | `/api/v1/auth/seed-demo-dept` | — | Seed demo data |
| `POST` | `/api/v1/complaints/text` | — | Submit text complaint |
| `POST` | `/api/v1/complaints/voice` | — | Submit voice complaint (S3 key) |
| `GET` | `/api/v1/complaints` | JWT | List complaints (paginated) |
| `GET` | `/api/v1/complaints/{ref}` | JWT | Get complaint detail |
| `PATCH` | `/api/v1/complaints/{ref}/status` | JWT | Update status |
| `GET` | `/api/v1/track/{ref}` | — | Public tracking |
| `GET` | `/api/v1/dashboard/stats` | JWT | Dashboard statistics |
| `GET` | `/api/v1/dashboard/sla` | JWT | SLA metrics by department |
| `GET` | `/api/v1/review/queue` | JWT | Review queue |
| `POST` | `/api/v1/review/{ref}/reclassify` | JWT (reviewer) | Manual reclassify |
| `GET` | `/api/v1/routing/rules` | JWT | List routing rules |
| `POST` | `/api/v1/routing/rules` | JWT (super_admin) | Create routing rule |
| `GET` | `/api/v1/pipeline/status` | JWT | Pipeline health |
| `POST` | `/api/v1/ivr/webhook` | — | Exotel IVR webhook |

Interactive docs: `http://localhost:8000/api/docs`

---

## Running Tests

```bash
cd backend
pip install -r requirements.txt   # includes pytest, hypothesis, aiosqlite, fakeredis
pytest tests/ -v

# Run property-based tests only
pytest tests/test_pipeline_properties.py tests/test_router_properties.py -v

# Run with coverage
pytest tests/ --cov=app --cov-report=html
```

---

## Supported Languages

| Code | Language | Script |
|------|----------|--------|
| `hi` | Hindi | Devanagari |
| `bho` | Bhojpuri | Devanagari |
| `ta` | Tamil | Tamil |
| `te` | Telugu | Telugu |
| `bn` | Bengali | Bengali |
| `mr` | Marathi | Devanagari |
| `kn` | Kannada | Kannada |
| `ml` | Malayalam | Malayalam |
| `gu` | Gujarati | Gujarati |
| `or` | Odia | Odia |
| `pa` | Punjabi | Gurmukhi |
| `ur` | Urdu | Arabic |

---

## Post-Deploy Smoke Tests

```bash
BACKEND=https://your-backend.railway.app

# Health check
curl $BACKEND/api/health

# Seed demo data
curl -X POST $BACKEND/api/v1/auth/seed-demo-dept

# Submit a test complaint (Hindi)
curl -X POST $BACKEND/api/v1/complaints/text \
  -H "Content-Type: application/json" \
  -d '{"text": "hamare gaon mein handpump tuta hua hai pani nahi aa raha", "channel": "web"}'

# Track it
curl $BACKEND/api/v1/track/VLCR-IN-2026-xxxxxxxx
```

---

*VLCR v1.0.0 — FastAPI + Anthropic Claude + Bhashini — GitHub → Vercel/Railway Production*
