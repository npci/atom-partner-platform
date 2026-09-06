# Deployment Guide — Partner Platform

Standalone deployment guide for UPI ecosystem partners (banks, PSPs, TPAPs).
This platform is a **client** of a live NPCI Change Management instance — it
receives change notifications, runs your agents, and communicates back over the
A2A protocol.

---

## Table of Contents

1. [Platform Overview](#1-platform-overview)
2. [Prerequisites](#2-prerequisites)
3. [Option A: Docker Deployment (Recommended)](#3-option-a-docker-deployment-recommended)
   - [3.1 Quick Start](#31-quick-start)
   - [3.2 What Gets Deployed](#32-what-gets-deployed)
   - [3.3 Configuration](#33-configuration)
   - [3.4 Loading Your Capability Profile](#34-loading-your-capability-profile)
   - [3.5 Pulling the Embedding Model](#35-pulling-the-embedding-model)
   - [3.6 Rebuilding After Code Changes](#36-rebuilding-after-code-changes)
   - [3.7 Stopping and Restarting](#37-stopping-and-restarting)
   - [3.8 Viewing Logs](#38-viewing-logs)
4. [Option B: Native Deployment (No Docker)](#4-option-b-native-deployment-no-docker)
   - [4.1 Install PostgreSQL](#41-install-postgresql)
   - [4.2 Install Ollama](#42-install-ollama)
   - [4.3 Backend Setup](#43-backend-setup)
   - [4.4 Frontend Setup](#44-frontend-setup)
   - [4.5 Nginx (Optional Reverse Proxy)](#45-nginx-optional-reverse-proxy)
5. [First-Time Setup (Both Options)](#5-first-time-setup-both-options)
6. [Connecting to NPCI](#6-connecting-to-npci)
7. [Agent Framework](#7-agent-framework)
   - [7.1 Shipped Agents](#71-shipped-agents)
   - [7.2 Agent Configuration (agents.yaml)](#72-agent-configuration-agentsyaml)
   - [7.3 Replacing an Agent with Your Own Code](#73-replacing-an-agent-with-your-own-code)
   - [7.4 Hosting an Agent as a Remote Service](#74-hosting-an-agent-as-a-remote-service)
8. [RAG & Knowledge Base](#8-rag--knowledge-base)
9. [Environment Variable Reference](#9-environment-variable-reference)
10. [Production Hardening](#10-production-hardening)
11. [Verification Checklist](#11-verification-checklist)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Platform Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Partner Platform                       │
│                                                          │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────────┐ │
│  │ Frontend │   │   Backend    │   │   PostgreSQL     │ │
│  │ React 19 │──▶│   FastAPI    │──▶│ pgvector:pg16    │ │
│  │ Vite     │   │   Python 3.12│   │ (vectors + RAG)  │ │
│  │ :3001    │   │   :8001      │   │                  │ │
│  └──────────┘   └──────┬───────┘   └──────────────────┘ │
│                        │                                  │
│                        ▼                                  │
│                 ┌──────────────┐                          │
│                 │   Ollama     │                          │
│                 │ nomic-embed  │                          │
│                 │ (768-dim)    │                          │
│                 └──────────────┘                          │
└─────────────────────────┬────────────────────────────────┘
                          │ A2A Protocol (JSON-RPC)
                          ▼
                  ┌───────────────┐
                  │ NPCI Platform │
                  └───────────────┘
```

**Components:**

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Backend | Python 3.12, FastAPI, SQLAlchemy | API server, agent orchestration, A2A protocol |
| Frontend | React 19, Vite, TanStack Query | Partner dashboard UI |
| PostgreSQL | Postgres 16 + pgvector | Data persistence + vector embeddings for RAG |
| Ollama | nomic-embed-text (768-dim) | Local embedding model for document/code search |

**Features:**

| Feature | Description |
|---------|-------------|
| Change Inbox | Receive UPI change notifications from NPCI with full Product Kit |
| Product Kit Viewer | View/download BRD, Tech Spec, FAQ, test cases |
| Feasibility Analysis | Auto-assess each change against your capability profile (pluggable agent) |
| Query & Negotiation | Ask NPCI questions, accept/counter rollout terms |
| Progress & Readiness | Report Design → Coding → Testing milestones, declare cert-ready |
| Agent Framework | Plug in your own agents (in-process Python or remote HTTP service) |
| RAG Search | Semantic search over ingested documents and partner code repos |

---

## 2. Prerequisites

### Docker deployment

| Requirement | Version | Notes |
|---|---|---|
| Docker Engine | 20.10+ | With Docker Compose v2 plugin |
| Disk space | 5 GB+ | Images + Postgres data + Ollama model cache |
| RAM | 4 GB+ | 8 GB recommended if running LLM-powered agents |

### Native deployment

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Backend runtime |
| Node.js | 20+ | Frontend build |
| PostgreSQL | 16 | With pgvector extension installed |
| Ollama | 0.3+ | Embedding model server |
| nginx | 1.24+ | Optional — reverse proxy for production |

### Both options

| Requirement | Notes |
|---|---|
| NPCI Platform URL | A reachable NPCI Change Management instance |
| NPCI-issued credentials | Partner API Key, JWT secret, HMAC secret (from NPCI onboarding) |
| LLM API Key (optional) | Anthropic, OpenAI, or AiNxt — without one, agents return mock output |

---

## 3. Option A: Docker Deployment (Recommended)

### 3.1 Quick Start

```bash
cd partner-platform

# 0. TLS — generate a self-signed dev cert (Finding 16:
#    docs/adr's ARCHITECTURE_REVIEW_ACTIONS.md). The `edge` service refuses to
#    start without deploy/tls/{cert,key}.pem. Replace with a real cert (from
#    your CA, or cert-manager/ACME) for anything beyond local dev.
mkdir -p deploy/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/tls/key.pem -out deploy/tls/cert.pem \
  -subj "/CN=localhost"

# 1. Configure
cp backend/.env.example backend/.env
# Edit backend/.env — at minimum set SESSION_JWT_SECRET and PARTNER_SECRET_KEK
# to random values (see §3.3 below for how to generate PARTNER_SECRET_KEK)

# 2. Build and start
docker compose up -d

# 3. Pull the embedding model (first time only — ~274 MB)
docker exec partner_ollama ollama pull nomic-embed-text

# 4. Open the UI (self-signed cert -> your browser will warn once; expected for local dev)
open https://localhost:8443/a2a-partner/
```

Default login: **admin** / **Admin@1234** (change immediately after first login).

### 3.2 What Gets Deployed

`docker compose up -d` starts five containers:

| Container | Image | Port | Purpose |
|---|---|---|---|
| `partner_postgres` | pgvector/pgvector:pg16 | Internal only | PostgreSQL + pgvector |
| `partner_ollama` | ollama/ollama:latest | Internal only | Embedding model server |
| `partner_backend` | Built from `backend/Dockerfile` | Internal only (behind `edge`) | FastAPI backend |
| `partner_frontend` | Built from `frontend/Dockerfile` | Internal only (behind `edge`) | React SPA via nginx |
| `partner_edge` | `nginx:1.27-alpine` | `8443` (HTTPS), `8080` (HTTP→HTTPS redirect) | TLS termination, fronts both `frontend` and `backend` |

**Why an edge proxy is in the default stack (Finding 16):** publishing the
backend's own port directly to the host (the platform's previous default)
meant both the operator UI login and the NPCI-facing A2A endpoint were
reachable over plaintext HTTP, bypassing every nginx-tier control a partner
might separately add. `edge` terminates TLS and is the ONLY container with a
host-published port — `backend` and `frontend` are reachable solely over the
internal compose network. Override the published ports via
`PARTNER_EDGE_HTTPS_PORT`/`PARTNER_EDGE_HTTP_PORT` if 8443/8080 conflict with
something else on your host.

**Persistent volumes:**

| Volume | Purpose |
|---|---|
| `partner_pg_data` | PostgreSQL data (survives container recreation) |
| `partner_ollama_data` | Cached embedding model (~274 MB) |
| `partner_data` | Agent output artifacts |

### 3.3 Configuration

Edit `backend/.env` before starting:

```bash
# ── Required ─────────────────────────────────────────────
SESSION_JWT_SECRET=<random-32-char-string>   # REQUIRED — no default; prod won't start unset

# Secrets-at-rest encryption key (docs/adr/ADR-0002-secrets-vault-migration.md).
# REQUIRED before saving any secret via Settings (NPCI JWT/HMAC secrets, LLM
# key, GitLab token) — encrypt/decrypt raises a clear error if unset.
# Generate with:
#   python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
PARTNER_SECRET_KEK=<32-byte-base64-key>

# ── Database (defaults match the docker-compose Postgres) ─
DATABASE_URL=postgresql+psycopg://partner_user:partner_password@partner_postgres:5432/partner_db

# ── Display ──────────────────────────────────────────────
PARTNER_NAME=Your Bank Name

# ── LLM (optional — mock output if empty) ────────────────
LLM_PROVIDER=claude
PARTNER_ANTHROPIC_API_KEY=sk-ant-...
# Or: LLM_PROVIDER=openai  +  PARTNER_OPENAI_API_KEY=sk-...
# Or: LLM_PROVIDER=ainxt   +  PARTNER_AINXT_API_KEY=...  + AINXT_BASE_URL=https://...

# ── Embeddings (defaults match the docker-compose Ollama) ─
OLLAMA_URL=http://ollama:11434
EMBED_MODEL=nomic-embed-text
EMBED_DIM=768
```

Docker Compose also accepts these env vars (set in shell or a root `.env`):

| Variable | Default | Notes |
|---|---|---|
| `PARTNER_POSTGRES_USER` | partner_user | Postgres superuser |
| `PARTNER_POSTGRES_PASSWORD` | partner_password | Postgres password |
| `PARTNER_POSTGRES_DB` | partner_db | Database name |
| `PARTNER_NAME` | Partner Agent | Display name |
| `SESSION_JWT_SECRET` | (empty) | **Required** — production refuses to start without it |
| `LLM_PROVIDER` | claude | `claude` / `openai` / `ainxt` |
| `PARTNER_ANTHROPIC_API_KEY` | (empty) | LLM key |
| `NPCI_PLATFORM_URL` | http://host.docker.internal | NPCI URL the connectivity probe targets |
| `NPCI_SSRF_ALLOWED_HOSTS` | (empty) | Hosts approved to resolve into private space — see below |
| `NPCI_SSRF_ALLOW_PRIVATE_NETWORKS` | false | Blanket approval for private space — see below |
| `PARTNER_ALLOW_HTTP` | true | Permits cleartext `http://` URLs. Set `false` (or unset) once every URL is `https://` |
| `SAFE_REDIRECT_HOSTS` | (empty) | Hosts the UI may link out to, e.g. your GitLab. **Build-time** — see below |

#### Merge-request links render as `#`

`SAFE_REDIRECT_HOSTS` is the allowlist behind `safeUrl.js`, which rebuilds every
outbound link from a validated host rather than trusting the URL it was given
(this is the fix for the DOM XSS and open-redirect findings). Any host not on the
list collapses to `#`, so until you set it, GitLab merge-request links are inert.

```bash
SAFE_REDIRECT_HOSTS=gitlab.example.com
```

Compose passes it into the image as the `VITE_SAFE_REDIRECT_HOSTS` build arg.
Note that this one is consumed at **build** time, unlike every other variable in
the table above — Vite inlines it into the JavaScript bundle, so changing it
requires a rebuild, not just a restart:

```bash
SAFE_REDIRECT_HOSTS=gitlab.example.com docker compose up -d --build frontend
```

Building the production image directly takes the same value as a build arg:

```bash
docker build -f Dockerfile.prod \
  --build-arg VITE_SAFE_REDIRECT_HOSTS=gitlab.internal -t partner-frontend:prod .
```

Same-origin links always work and need no entry. List multiple hosts
comma-separated.

> **Docker only:** set these in a root `.env` or your shell, *not* in
> `backend/.env`. The backend image excludes that file (`.dockerignore`) and the
> service declares no `env_file`, so values placed there never reach the
> container. A native deployment is the opposite — it reads `backend/.env`
> directly (§4.3).

#### Test Connection says "resolves to a private or reserved IP address"

The connectivity probe refuses any URL that resolves into private address space,
so admin access to Settings cannot be turned into an internal port scanner. NPCI's
UAT and production platforms normally sit on RFC-1918 space, so the refusal is
expected until you approve the target explicitly — there is no public endpoint to
switch to.

Approve the specific host (preferred — narrowest grant):

```bash
NPCI_SSRF_ALLOWED_HOSTS=10.84.12.34,npci-uat.internal
```

Or approve private space wholesale, when enumerating hosts is impractical:

```bash
NPCI_SSRF_ALLOW_PRIVATE_NETWORKS=true
```

**Where to put it depends on how you deploy:**

| Deployment | Location | Apply with |
|---|---|---|
| Native (no Docker) | `backend/.env` | restart `uvicorn` |
| Docker Compose | root `.env` or shell — **not** `backend/.env`, which the image excludes | `docker compose up -d --force-recreate backend` |

Loopback and link-local stay blocked either way. Neither setting re-enables the
cloud metadata endpoint (169.254.169.254), which is never a legitimate NPCI
platform. Match the host as it appears in the URL: `NPCI_SSRF_ALLOWED_HOSTS`
compares against the URL's hostname or IP literal, not the resolved address, so
approve the hostname when you configure a hostname.

Note the probe checks two URLs — `npci_platform_url` (reachability) and
`npci_a2a_url` (the A2A round-trip, set from the Settings UI). If they are
different hosts, both need approval.

### 3.4 Loading Your Capability Profile

The feasibility agent reads a capability profile to assess whether your
organisation can implement a proposed UPI change. The default compose mounts
the blank template. To load a real profile:

**Option 1 — Edit the template in place:**
```bash
# Edit the template with your bank's details
vi data/partner_profile.template.md
docker compose restart backend
```

**Option 2 — Mount a custom file:**

Edit `docker-compose.yml` and change the profile mount:

```yaml
volumes:
  - ./data/my_bank_profile.md:/app/data/partner_profile.md:ro
```

Then `docker compose up -d backend`.

**Option 3 — Use the HDFC example as a starting point:**

```yaml
volumes:
  - ./data/examples/hdfc_profile.md:/app/data/partner_profile.md:ro
```

The profile template has 11 sections covering identity, tech stack, API patterns,
channels, vendor map, operational envelope, implementation patterns, constraints,
recent rollouts, regulatory posture, and org capabilities. Fill in what you can —
the agent works with partial profiles.

### 3.5 Pulling the Embedding Model

The Ollama container starts empty. Pull the embedding model once:

```bash
docker exec partner_ollama ollama pull nomic-embed-text
```

The model is cached in the `partner_ollama_data` volume and persists across restarts.

**Verify:**
```bash
docker exec partner_ollama ollama list
# NAME                    SIZE
# nomic-embed-text:latest 274 MB
```

### 3.6 Rebuilding After Code Changes

Backend source is baked into the Docker image. After editing Python code:

```bash
# CORRECT — rebuilds the image and recreates the container
docker compose up -d --build backend

# WRONG — reuses the old image with stale code
docker compose restart backend
```

Same for the frontend:
```bash
docker compose up -d --build frontend
```

### 3.7 Stopping and Restarting

```bash
# Stop everything (data volumes preserved)
docker compose down

# Start again
docker compose up -d

# Full reset (destroys all data)
docker compose down -v
```

### 3.8 Viewing Logs

```bash
# All services
docker compose logs -f

# Single service
docker compose logs -f backend
docker compose logs -f partner_postgres

# Last 50 lines
docker compose logs --tail=50 backend
```

---

## 4. Option B: Native Deployment (No Docker)

### 4.1 Install PostgreSQL

PostgreSQL 16 with the pgvector extension is required.

**macOS:**
```bash
brew install postgresql@16
brew install pgvector
brew services start postgresql@16

# Create the database
createdb partner_db
psql partner_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -c "CREATE USER partner_user WITH PASSWORD 'partner_password';"
psql -c "GRANT ALL PRIVILEGES ON DATABASE partner_db TO partner_user;"
psql -c "ALTER DATABASE partner_db OWNER TO partner_user;"
# Grant schema permissions (required for table creation)
psql partner_db -c "GRANT ALL ON SCHEMA public TO partner_user;"
```

**Ubuntu/Debian:**
```bash
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl enable postgresql
sudo systemctl start postgresql

sudo -u postgres createdb partner_db
sudo -u postgres psql partner_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql -c "CREATE USER partner_user WITH PASSWORD 'partner_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE partner_db TO partner_user;"
sudo -u postgres psql partner_db -c "GRANT ALL ON SCHEMA public TO partner_user;"
```

**RHEL/CentOS:**
```bash
sudo dnf install -y postgresql16-server postgresql16-contrib
sudo postgresql-setup --initdb
sudo systemctl enable postgresql
sudo systemctl start postgresql

# Install pgvector from source
git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git
cd pgvector
make && sudo make install

sudo -u postgres createdb partner_db
sudo -u postgres psql partner_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql -c "CREATE USER partner_user WITH PASSWORD 'partner_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE partner_db TO partner_user;"
sudo -u postgres psql partner_db -c "GRANT ALL ON SCHEMA public TO partner_user;"
```

**Verify:**
```bash
psql -U partner_user -d partner_db -c "SELECT extname FROM pg_extension;"
# Should list: plpgsql, vector
```

### 4.2 Install Ollama

Ollama serves the embedding model locally. No data leaves your network.

**macOS:**
```bash
brew install ollama
ollama serve &                        # starts on http://localhost:11434
ollama pull nomic-embed-text          # download the model (~274 MB)
```

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable ollama
sudo systemctl start ollama
ollama pull nomic-embed-text
```

**Verify:**
```bash
curl -s http://localhost:11434/api/tags | python3 -m json.tool
# Should list nomic-embed-text
```

### 4.3 Backend Setup

```bash
cd partner-platform/backend

# 1. Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# Required
SESSION_JWT_SECRET=<random-32-char-string>
DATABASE_URL=postgresql+psycopg://partner_user:partner_password@localhost:5432/partner_db

# Display
PARTNER_NAME=Your Bank Name

# LLM (optional — feasibility returns mock output without a key)
LLM_PROVIDER=claude
PARTNER_ANTHROPIC_API_KEY=sk-ant-...

# Embeddings
OLLAMA_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text
EMBED_DIM=768

# Capability profile
PARTNER_PROFILE_PATH=../data/partner_profile.template.md

# Agent manifest
AGENTS_CONFIG=config/agents.yaml

# NPCI target + SSRF approval for the Test Connection probe.
# NPCI UAT/production sit on private (RFC-1918) space, which the probe refuses
# until the host is explicitly approved — see the troubleshooting note below.
NPCI_PLATFORM_URL=https://npci-uat.internal
NPCI_SSRF_ALLOWED_HOSTS=npci-uat.internal

# The address NPCI posts A2A traffic to. Defaults to a docker-compose service
# name, so a native install MUST set it.
PARTNER_PUBLIC_URL=https://partner.example.com/a2a-partner

# The service refuses to start if any of npci_platform_url / partner_public_url
# / ollama_url is cleartext http://. A native install usually runs Ollama on
# loopback, which is http:// — enable this for development only, and prefer
# https:// everywhere real credentials flow.
PARTNER_ALLOW_HTTP=true
```

> Every value above belongs in `backend/.env`, which is read from the process
> working directory — start `uvicorn` from `partner-platform/backend`, or the
> file is not found and the defaults (docker-compose hostnames) apply.

```bash
# 4. Start the backend
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

On first startup, `init_db()` automatically:
- Creates all database tables
- Enables the pgvector extension
- Creates the vector embedding tables
- Seeds the default admin user (`admin` / `Admin@1234`)
- Loads the capability profile from `PARTNER_PROFILE_PATH`

The backend is now running at **http://localhost:8001**.

### 4.4 Frontend Setup

```bash
cd partner-platform/frontend

# 1. Install dependencies
npm ci                              # or: npm install

# 2. Development mode (with hot reload + API proxy)
npm run dev
# Runs on http://localhost:3001
# Proxies /api/* → http://localhost:8001
```

For a production build:
```bash
npm run build
# Output: dist/ directory — serve with nginx or any static server
```

The frontend is now running at **http://localhost:3001**.

### 4.5 Nginx (Optional Reverse Proxy)

For production, put nginx in front of both services:

```bash
# macOS
brew install nginx

# Ubuntu/Debian
sudo apt install -y nginx
```

Create `/etc/nginx/conf.d/partner.conf`:

```nginx
upstream partner_backend  { server 127.0.0.1:8001; }
upstream partner_frontend { server 127.0.0.1:3001; }

server {
    listen 80;
    server_name _;
    client_max_body_size 50M;

    # Root redirect
    location = / { return 302 /a2a-partner/; }

    # Frontend (SPA)
    location /a2a-partner/ {
        proxy_pass http://partner_frontend/;
    }

    # API
    location /a2a-partner/api/ {
        proxy_pass http://partner_backend;
        rewrite ^/a2a-partner/(.*)$ /$1 break;
        proxy_read_timeout 300s;
    }

    # A2A protocol endpoints (NPCI calls these)
    location /a2a-rpc/ {
        proxy_pass http://partner_backend;
        proxy_read_timeout 60s;
    }

    location /.well-known/ {
        proxy_pass http://partner_backend;
    }
}
```

```bash
# Start / reload nginx
sudo systemctl restart nginx
```

Access the platform at **http://your-host/a2a-partner/**.

---

## 5. First-Time Setup (Both Options)

After starting all components:

1. Open the UI:
   - Docker: https://localhost:8443/a2a-partner/ — the `edge` proxy is the only
     published port; the backend and frontend are not exposed to the host
   - Native with nginx: http://localhost/a2a-partner/
   - Native without nginx: http://localhost:3001

2. **Log in** with default credentials:
   - Username: `admin`
   - Password: `<your-default-admin-password>` (set via `ADMIN_PASSWORD` env var, or auto-generated on first boot)
   - **Change the password immediately.**

3. Open **Settings** and enter:
   - **NPCI Platform URL** — the NPCI Change Management instance URL
   - **Partner API Key** — issued by NPCI during onboarding
   - **NPCI JWT Secret** — shared secret for verifying inbound A2A tokens
   - **NPCI HMAC Secret** — shared secret for verifying request signatures
   - **Partner Name** — your organisation's display name

4. Click **Test Connection** to verify NPCI reachability, then **Save**.

---

## 6. Connecting to NPCI

The partner platform communicates with NPCI over the A2A protocol:

### Inbound (NPCI → Partner)

NPCI calls your platform at these endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/agent-card.json` | Agent card discovery (advertises your capabilities) |
| `POST /a2a-rpc/rpc` | JSON-RPC — receives `change_communication`, `clarification_response` |

**Authentication:** NPCI sends a Bearer JWT (HS256, signed with the shared
`npci_jwt_secret`) and an HMAC envelope (`X-NPCI-Signature`). The platform
validates both automatically via Tier 1 middleware.

### Outbound (Partner → NPCI)

Your platform sends messages to NPCI's A2A endpoint:

| Skill | Purpose |
|---|---|
| `query` | Submit an implementation question to NPCI |
| `progress` | Report a milestone (design_completed / coding_completed / testing_completed) |
| `readiness` | Declare ready for certification testing |

**Discovery:** The platform discovers NPCI's endpoint via its agent card at
`{npci_url}/.well-known/agent-card.json`.

### Network Requirements

Your platform needs:
- **Inbound:** NPCI must be able to reach your `/a2a-rpc/rpc` endpoint (open firewall / VPN)
- **Outbound:** Your platform must reach the NPCI A2A endpoint (typically `https://npci-host/a2a-rpc/`)

---

## 7. Agent Framework

### 7.1 Shipped Agents

Five agents ship with the platform. Four have full LLM-powered implementations;
one is a stub for you to replace:

| Agent | Module | Status | Runs when |
|---|---|---|---|
| `feasibility` | `app/agents/feasibility.py` | **Real** — LLM analysis against your capability profile | A new change arrives from NPCI |
| `design` | `app/agents/design.py` | **Real** — produces a structured design document (posture, sections, risks) | User triggers Design phase |
| `code` | `app/agents/code.py` | **Real** — implementation plan + change skeleton; repo-grounded when Code RAG is configured | User triggers Code phase |
| `test` | `app/agents/testing.py` | **Real** — test plan + cert coverage mapping against NPCI test cases | User triggers Test phase |
| `negotiation` | `app/agents/negotiation.py` | **Stub** — returns mock output; replace with your drafting logic | Counter-proposal / negotiation |

All four real agents follow the same pattern:
- Read your capability profile (`PARTNER_PROFILE_PATH`)
- Assemble context from the change's product-kit documents, prior phase outputs, and RAG-retrieved knowledge
- Call the configured LLM (`call_llm()` with 64k max_tokens)
- Parse and validate the structured JSON response
- **If no LLM key is configured**, return a schema-valid mock marked `_meta.mock=True` — the platform works end-to-end without credentials

Every agent run is audited in the `agent_runs` database table (agent name, status,
latency, input summary, output, errors).

### 7.2 Agent Configuration (agents.yaml)

Agents are wired in `config/agents.yaml` (mounted read-only into the container):

```yaml
agents:
  feasibility:
    impl: app.agents.feasibility:FeasibilityAgent
    prompt: feasibility.md
    enabled: true
    # provider: claude          # optional per-agent LLM provider override
    # model: claude-opus-4-8    # optional per-agent model override

  design:
    impl: app.agents.design:DesignAgent
    prompt: design.md
    enabled: true

  code:
    impl: app.agents.code:CodeAgent
    prompt: code.md
    enabled: true

  test:
    impl: app.agents.testing:TestAgent
    prompt: test.md
    enabled: true

  negotiation:
    impl: app.agents.negotiation:NegotiationAgent
    prompt: negotiation.md
    enabled: true
```

**Binding types:**

| Key | How it works |
|---|---|
| `impl: module:ClassName` | In-process Python class (default, ships out of the box) |
| `url: https://your-host/agents/code` | Remote HTTP service you host (any language) |

### 7.3 Replacing an Agent with Your Own Code

The simplest path — edit the shipped agent in place:

1. Open `app/agents/design.py`
2. Replace the body of the `run()` method with your logic
3. Edit the prompt at `app/agents/prompts/design.md`
4. Restart the backend

```python
# app/agents/design.py
from app.agents.base import Agent

class DesignAgent(Agent):
    def run(self, input: dict) -> dict:
        # Your logic here — input has change details, profile, etc.
        # Return a dict with your design report
        return {
            "status": "completed",
            "report": "...",
            "recommendations": [...]
        }
```

To add a brand-new agent:

1. Create `app/agents/myagent.py` with `class MyAgent(Agent)`
2. Create `app/agents/prompts/myagent.md`
3. Register in `config/agents.yaml`:
   ```yaml
   myagent:
     impl: app.agents.myagent:MyAgent
     prompt: myagent.md
     enabled: true
   ```
4. Restart the backend

### 7.4 Hosting an Agent as a Remote Service

Deploy your agent as an HTTP service (any language/framework) and point the
manifest at it. No code changes to the platform.

**HTTP contract your service must implement:**

```
POST {url}/run
  Headers:
    Authorization: Bearer <AGENT_SERVICE_TOKEN>
    X-Correlation-Id: <uuid>
    Content-Type: application/json
  Body:
    {
      "agent": "<name>",
      "input": { ... },
      "change_id": "<id or null>",
      "metadata": {}
    }

Response:
  200  {"status": "ok", "output": { ... }}
  4xx  {"status": "error", "error": "reason"}

GET {url}/health
  200  (any body)
```

**Update agents.yaml:**

```yaml
code:
  url: https://your-internal-host/agents/code
  auth: bearer
  timeout_s: 30
  retries: 2
```

Set `AGENT_SERVICE_TOKEN` in `.env` to the bearer token your service expects.

---

## 8. RAG & Knowledge Base

The platform includes a vector-powered retrieval system using pgvector + Ollama.

**How it works:**
1. Documents (PDFs, markdown, code files) are chunked and embedded using
   `nomic-embed-text` (768 dimensions)
2. Embeddings are stored in the `document_chunks` table (pgvector)
3. Agents query the vector store for relevant context during analysis

**Code RAG** (optional): If you set `partner_gitlab_url` and provide GitLab
credentials, the platform can index your source code repository for the code
agent to reference during analysis.

---

## 9. Environment Variable Reference

### Backend (`backend/.env`)

**Required:**

| Variable | Description |
|---|---|
| `SESSION_JWT_SECRET` | Login session signing key — **must be unique per deployment** |
| `PARTNER_SECRET_KEK` | 32-byte base64 key-encryption-key for secrets-at-rest (NPCI JWT/HMAC secrets, LLM key, GitLab token) — **must be set before saving any secret via Settings**. See `docs/adr/ADR-0002-secrets-vault-migration.md`. |
| `DATABASE_URL` | PostgreSQL connection string |

**Security (optional, default-safe):**

| Variable | Default | Description |
|---|---|---|
| `PARTNER_ALLOW_UNAUTHENTICATED_A2A` | `false` | Dev-only escape hatch — when `true`, the A2A ingress accepts unauthenticated calls if NPCI secrets aren't configured yet. **NEVER set this in production.** See `docs/adr/ADR-0003-fail-closed-a2a-ingress.md`. |
| `A2A_MAX_REQUEST_BODY_BYTES` | 10485760 (10 MB) | Inbound A2A request size cap |
| `A2A_RATE_LIMIT_RPS` | 20 | A2A ingress requests-per-second cap |
| `LLM_MAX_CONCURRENT_CALLS` | 8 | Bulkhead limit on concurrent LLM provider calls |

**Display & Identity:**

| Variable | Default | Description |
|---|---|---|
| `PARTNER_NAME` | Partner Agent | Organisation display name |
| `NPCI_PLATFORM_URL` | http://localhost | NPCI instance URL (for connectivity test) |
| `PARTNER_API_KEY` | (empty) | NPCI-issued bootstrap key (overridden from Settings UI) |

**LLM Provider:**

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | claude | `claude` / `openai` / `ainxt` |
| `PARTNER_ANTHROPIC_API_KEY` | (empty) | Anthropic API key (mock output if unset) |
| `CLAUDE_MODEL` | claude-sonnet-4-6 | Claude model ID |
| `PARTNER_OPENAI_API_KEY` | (empty) | OpenAI API key |
| `OPENAI_MODEL` | gpt-4o-mini | OpenAI model ID |
| `PARTNER_AINXT_API_KEY` | (empty) | AiNxt gateway key |
| `AINXT_BASE_URL` | https://gateway.example.com/ainxt/v1/api | AiNxt endpoint |
| `AINXT_MODEL` | gpt-4o | AiNxt model ID |

**Embeddings:**

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | http://ollama:11434 | Ollama server URL |
| `EMBED_MODEL` | nomic-embed-text | Embedding model name |
| `EMBED_DIM` | 768 | Embedding vector dimension |

**Agent Framework:**

| Variable | Default | Description |
|---|---|---|
| `AGENTS_CONFIG` | /app/config/agents.yaml | Path to agent manifest |
| `AGENT_SERVICE_TOKEN` | (empty) | Bearer token for remote `url:` agents |
| `PARTNER_PROFILE_PATH` | /app/data/partner_profile.md | Capability profile path |

### Docker Compose overrides (root `.env` or shell)

| Variable | Default | Description |
|---|---|---|
| `PARTNER_POSTGRES_USER` | partner_user | Postgres username |
| `PARTNER_POSTGRES_PASSWORD` | partner_password | Postgres password |
| `PARTNER_POSTGRES_DB` | partner_db | Database name |

---

## 10. Production Hardening

Before deploying to production:

### 10.1 Secrets

- [ ] Set `SESSION_JWT_SECRET` to a cryptographically random string (32+ chars)
- [ ] Set `PARTNER_SECRET_KEK` to a cryptographically random 32-byte base64 key (see §3.3) — required before any NPCI/LLM/GitLab secret can be saved via Settings
- [ ] Set strong Postgres password (change `PARTNER_POSTGRES_PASSWORD`)
- [ ] Set `APP_ENV=production` (the app will refuse to start with the default JWT secret)
- [ ] Leave `PARTNER_ALLOW_UNAUTHENTICATED_A2A` unset/`false` (the default) — this must NEVER be `true` in production; see `docs/adr/ADR-0003-fail-closed-a2a-ingress.md`

### 10.2 TLS

The default `docker-compose.yml` already terminates TLS at the `edge` service
(Finding 16) — this is no longer an opt-in step:

- [ ] Replace the self-signed dev cert at `deploy/tls/{cert,key}.pem` (generated in §3.1) with a real certificate from your CA, a load balancer's managed cert, or cert-manager/ACME
- [ ] Point NPCI's outbound A2A calls at `https://<your-host>:8443/a2a-rpc/rpc` (or your externally reachable port/hostname) — the `edge` container is the sole TLS-terminating, host-published entry point
- [ ] If you front `edge` with your own load balancer/CDN instead of publishing its port directly, ensure TLS is terminated at or before that layer — `backend` and `frontend` are intentionally NOT reachable directly (`expose:`, not `ports:`)

### 10.3 Network

- [ ] Restrict Postgres to internal network only (remove host port binding — already the default in `docker-compose.yml`, not exposed)
- [ ] Restrict Ollama to internal network only (already the default — not exposed)
- [ ] `backend` and `frontend` are already internal-only (`expose:`, not `ports:`) — only `edge` (443/80, mapped to `8443`/`8080` by default) is host-published
- [ ] Allow inbound from NPCI's IP range to your `edge` endpoint's HTTPS port
- [ ] Allow outbound to NPCI's A2A endpoint
- [ ] Do not add a `ports:` entry back onto `backend` or `frontend` — doing so re-introduces the direct, TLS-bypassing exposure Finding 16 closed

### 10.4 Backups

```bash
# Database backup
docker exec partner_postgres pg_dump -U partner_user partner_db > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i partner_postgres psql -U partner_user partner_db < backup_20260616.sql
```

### 10.5 Resource Limits (Docker)

Add resource limits to `docker-compose.yml`:

```yaml
backend:
  deploy:
    resources:
      limits:
        memory: 2G
        cpus: '2'
```

### 10.6 Production Docker Compose

For air-gapped environments with an internal container registry, use
`backend/Dockerfile.prod` and `frontend/Dockerfile.prod` which pull from
your private registry (`$REGISTRY`) instead of Docker Hub:

```yaml
backend:
  build:
    context: ./backend
    dockerfile: Dockerfile.prod
frontend:
  build:
    context: ./frontend
    dockerfile: Dockerfile.prod
```

---

## 11. Verification Checklist

Run these after deployment to confirm everything works:

### Health checks

The URLs differ between the two deployment options: under Docker everything is
reached through the `edge` proxy, which is the only published port. Natively the
services are on their own ports.

```bash
# Backend health          — Docker
curl -sk https://localhost:8443/a2a-partner/api/health
#                         — native
curl -s  http://localhost:8011/api/health
# Expected: {"status":"ok","partner":"Your Bank Name"}

# Frontend loads          — Docker
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/a2a-partner/
#                         — native
curl -s  -o /dev/null -w '%{http_code}\n' http://localhost:3001
# Expected: 200

# Postgres reachable
docker exec partner_postgres pg_isready -U partner_user
# Expected: accepting connections

# Ollama model loaded
docker exec partner_ollama ollama list
# Expected: nomic-embed-text listed
```

### Login test

```bash
curl -s -X POST http://localhost:8011/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "Admin@1234"}'
# Expected: JSON with "access_token"
```

### Agent card

```bash
curl -s http://localhost:8011/.well-known/agent-card.json | python3 -m json.tool
# Expected: JSON with name, skills, endpoint URL
```

### Database tables created

```bash
docker exec partner_postgres psql -U partner_user -d partner_db \
  -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"
# Expected: agent_jobs, agent_runs, change_documents, incoming_changes, etc.
```

---

## 12. Troubleshooting

### Backend won't start: "SESSION_JWT_SECRET is unset"

There is no default — production refuses to start without it. Set a real secret
in `backend/.env`:
```bash
SESSION_JWT_SECRET=$(openssl rand -hex 16)
```

### Settings save fails with "PARTNER_SECRET_KEK is unset"

Secrets saved via Settings (NPCI JWT/HMAC secrets, LLM key, GitLab token) are
encrypted at rest and require a key-encryption-key. Set one in `backend/.env`:
```bash
PARTNER_SECRET_KEK=$(python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
```
Then restart the backend and re-save the secret from the Settings UI.

### A2A calls return 503 "envelope_not_configured" / "jwt_not_configured"

This is the platform's fail-closed default (`docs/adr/ADR-0003-fail-closed-a2a-ingress.md`):
inbound A2A calls are rejected until `npci_hmac_secret` and `npci_jwt_secret`
are installed in Settings. This is expected behavior for a freshly deployed,
not-yet-onboarded instance — complete the Settings-UI onboarding step (§5) to
resolve it. Do **not** set `PARTNER_ALLOW_UNAUTHENTICATED_A2A=true` to work
around this outside of local development.

### "relation does not exist" errors

The tables are created on first startup via `init_db()`. If Postgres wasn't
ready when the backend started:
```bash
docker compose restart backend
```

### Ollama embedding errors in logs

The model must be pulled first:
```bash
# Docker
docker exec partner_ollama ollama pull nomic-embed-text

# Native
ollama pull nomic-embed-text
```

### Frontend shows blank page at localhost:3001

Check the backend is running (the frontend depends on it):
```bash
docker compose logs backend --tail=20
```

If using the production build (`npm run build`), the SPA needs a server with
fallback routing — serve `dist/` with nginx, not a bare file server.

### "Connection refused" when NPCI sends A2A messages

- Verify your platform is reachable from NPCI's network
- Check the agent card URL in Settings matches your externally reachable URL
- Ensure `/a2a-rpc/rpc` and `/.well-known/agent-card.json` are not blocked by
  firewall rules

### Code changes not reflected after restart

Backend source is baked into the Docker image:
```bash
# CORRECT
docker compose up -d --build backend

# WRONG
docker compose restart backend
```

### Feasibility returns mock output despite having an LLM key

- Verify `LLM_PROVIDER` matches your key type (`claude` for Anthropic, `openai`
  for OpenAI, `ainxt` for AiNxt)
- Verify the key variable name is correct: `PARTNER_ANTHROPIC_API_KEY` (not
  `ANTHROPIC_API_KEY` — the `PARTNER_` prefix is required)
- Check backend logs for LLM call errors:
  ```bash
  docker compose logs backend | grep -i "llm\|anthropic\|error"
  ```

### Postgres connection fails from native backend

Verify the connection string uses the correct driver:
```
DATABASE_URL=postgresql+psycopg://partner_user:partner_password@localhost:5432/partner_db
```

Note: the driver is `psycopg` (psycopg3), not `psycopg2`.

### Agent run fails with "agent not found"

Check `config/agents.yaml` has the agent registered and `enabled: true`. The
`AGENTS_CONFIG` env var must point to the correct file path.
