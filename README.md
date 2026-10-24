# Partner Platform — UPI Change Management

Reference base code for UPI ecosystem partners (banks, PSPs, TPAPs) to interact with NPCI's
Change Management Platform over the A2A protocol — and to **plug in their own agents**. Fork it,
configure it, and replace the agent logic with your own.

> This platform is a **client** of a live NPCI instance. It needs a reachable NPCI A2A endpoint
> plus NPCI-issued credentials (set from the Settings UI). It is not a standalone server.

## Quick start

```bash
cd partner-platform
cp backend/.env.example backend/.env     # set SESSION_JWT_SECRET; optionally an LLM key
docker compose up -d
```

Open <http://localhost:3001>. This brings up Postgres + the backend (`:8011`) + the frontend
(`:3001`). The five agents run in-process; with no LLM key the feasibility agent returns mock
output, so the stack works end-to-end on a fresh clone.

## First-time setup

1. Log in (default admin: `admin` / `Admin@1234` — change it immediately).
2. Open **Settings** and enter:
   - your **NPCI Platform URL**,
   - the **Partner API Key** and **NPCI JWT / HMAC secrets** issued to you during onboarding,
   - your **Partner Name**.
3. Click **Test Connection**, then **Save**.

## Features

| Feature | Description |
|---------|-------------|
| **Change Inbox** | Receive UPI feature change notifications from NPCI with the full Product Kit |
| **Product Kit Viewer** | View / download the kit docs (BRD, Tech Spec, FAQ, test cases, …) |
| **Feasibility** | Auto-assessment of each change against your capability profile (pluggable agent) |
| **Query / Negotiation** | Ask NPCI questions, accept/counter rollout terms |
| **Progress & Readiness** | Report Design → Coding → Testing, declare ready for certification |
| **Agent framework** | Plug in your own agents (in-process code or a service you host) |

## Architecture (short version)

Three tiers: **Contract** (`app/a2a_common/*` — the NPCI A2A wire, don't edit) → **Platform**
(transport, persistence, UI) → **Agent** (`app/agents/` — your plug-in zone). Agents are wired
in `config/agents.yaml` and reached either in-process (`impl:`) or over HTTP (`url:`). Every
agent run is audited in the `agent_runs` table.

- **Backend:** Python / FastAPI + PostgreSQL
- **Frontend:** React + Vite
- **Ports:** backend `8011` (→ container `8001`), frontend `3001`, Postgres internal

**See [`ARCHITECTURE.md`](./ARCHITECTURE.md)** for the full guide: the Agent contract, bindings,
config/secret split, LLM key & model resolution, the audit table, prompt customisation, the
remote-agent HTTP contract, and a "build your own agent" walkthrough.

## A2A protocol surface

- **Agent Card:** `GET /.well-known/agent-card.json`
- **JSON-RPC:** `POST /a2a-rpc/rpc` (NPCI sends change_communication / clarification_response;
  the partner sends query / progress / readiness)
- **Auth:** Bearer JWT signed by NPCI + an HMAC envelope (validated by the Tier-1 middleware)

## Running the backend without Docker

```bash
cd backend
python -m venv venv && . venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
# point DATABASE_URL at a reachable Postgres, set SESSION_JWT_SECRET, then:
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

Frontend: `cd frontend && npm install && npm run dev`.

## Tests & lint

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest        # agent-contract, handler smoke, and end-to-end tests
ruff check .
```

## Key environment variables

See `backend/.env.example` for the full list. Highlights:

| Variable | Purpose |
|----------|---------|
| `SESSION_JWT_SECRET` | Local login-session signing key — **set per deployment** |
| `DATABASE_URL` | Postgres connection string |
| `AGENTS_CONFIG` | Path to the agent manifest (`config/agents.yaml` by default) |
| `PARTNER_PROFILE_PATH` | Capability profile read by the feasibility agent |
| `LLM_PROVIDER` + `PARTNER_*_API_KEY` | LLM provider + key for in-process agents (optional) |
| `AGENT_SERVICE_TOKEN` | Bearer token for any agent configured with a remote `url:` |
