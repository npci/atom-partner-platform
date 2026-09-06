# Partner Platform — Architecture & Developer Guide

This is **reference base code** for UPI ecosystem partners (banks, PSPs, TPAPs) to
interact with NPCI's Change Management Platform over the A2A protocol — and to **plug in
their own agents**. Fork it, configure it, replace the agent bodies with your logic.

> The platform is a **client** of a live NPCI instance. It needs a reachable NPCI A2A
> endpoint plus NPCI-issued credentials (configured from the Settings UI). It is not a
> standalone server.

---

## The three tiers

```
TIER 1  CONTRACT   app/a2a_common/*   ── the NPCI A2A wire (JSON-RPC, JWT, HMAC).
                                          DO NOT EDIT. Keep in sync with NPCI on releases.
TIER 2  PLATFORM   executor · handlers/ · api/ · models · database · core/llm · frontend
                                          ── transport, persistence, UI. Rarely changed.
TIER 3  AGENT      app/agents/        ── YOUR plug-in zone: the agents + prompts.
```

Dependency direction is one-way: **Platform → Agent** (via the registry) and **Platform →
Contract**. The agent tier receives plain dicts and returns plain dicts — no DB/ORM objects
cross the boundary, so your agent stays decoupled and testable.

---

## The Agent contract

Every agent subclasses `app.agents.base.Agent`. You implement `run(input) -> output`; the
base class's `execute()` owns the lifecycle + the `agent_runs` audit row, so you never touch
the wiring:

```python
class Agent(ABC):
    def execute(self, input, *, db, change_id=None, user_id=None) -> dict:
        run = audit_start(...)        # agent_runs row, status=running
        self.before_run(input)        # lifecycle hook (optional)
        try:
            output = self.run(input)  # ← YOUR code
            self.after_run(input, output)
            audit_succeed(db, run, output); return output
        except Exception as e:
            audit_fail(db, run, e); raise

    @abstractmethod
    def run(self, input: dict) -> dict: ...
    def before_run(self, input): ...     # override if useful
    def after_run(self, input, output): ...
```

The orchestrator (handlers + API endpoints) only ever calls
`registry.get("<name>").execute(...)` — it never imports a concrete agent. That single seam
is what makes agents swappable.

### The seven shipped agents

| Name | Module | Status |
|------|--------|--------|
| `feasibility` | `app/agents/feasibility.py` | **Real** logic; returns a mock report when no LLM key is set |
| `design` | `app/agents/design.py` | **Real** — produces a structured design document; mock output when no LLM key is set |
| `code` | `app/agents/code.py` | **Real** — implementation plan + whole-file generation, repo-grounded when Code RAG is configured; mock output when no LLM key is set |
| `test` | `app/agents/testing.py` | **Real** — test plan + cert coverage mapping; mock output when no LLM key is set |
| `negotiation` | `app/agents/negotiation.py` | Stub — returns mock output; replace with your drafting logic |
| `code_reviewer` | `app/agents/code_reviewer.py` | **Real** — one of the two review lenses; see the review-gate section below |
| `security_reviewer` | `app/agents/security_reviewer.py` | **Real** — the second review lens; any finding blocks the merge request |

Each agent's input/output shape is documented in its prompt file under `app/agents/prompts/`.

---

## Bindings — how the platform reaches an agent

Configured per agent in `config/agents.yaml` (override the path with `AGENTS_CONFIG`):

```yaml
agents:
  feasibility:
    impl: app.agents.feasibility:FeasibilityAgent   # in-process Python class (default)
    prompt: feasibility.md
    enabled: true
    # provider: claude        # optional per-agent LLM provider override
    # model: claude-opus-4-8  # optional per-agent model override
```

- **`impl:`** — a shipped Python class, run **in-process**. This is how the reference agents
  ship and run out of the box.
- **`url:`** — a service **you host**, called over HTTP. Flip one line, no code change:
  ```yaml
  code: { url: https://bank-host/agents/code, auth: bearer, timeout_s: 30, retries: 2 }
  ```
- **`mcp:`** — *designed-for, not built this pass.* The manifest key + adapter seam are
  reserved; a `mcp:` entry currently raises a clear "not implemented" error at load. Add the
  `McpAgent` adapter when you need it — no rework to the rest.

### Hosting your own agent (the `url:` binding)

Implement this HTTP contract in any language and point the manifest at it:

```
POST {url}/run
  Authorization: Bearer <token>           # from $AGENT_SERVICE_TOKEN, when auth: bearer
  X-Correlation-Id: <uuid>
  body: {"agent": "<name>", "input": {...}, "change_id": "<id|null>", "metadata": {}}

200 -> {"status": "ok", "output": {...}}        (a bare output dict is also accepted)
4xx/5xx or {"status": "error", "error": "..."} -> recorded as a failed agent_run
GET {url}/health -> 200
```

**Two MCP layers — don't conflate them.** The `mcp:` *binding* above is the platform calling
your agent over MCP (deferred). Separately, whatever your agent does **internally** — including
acting as an MCP client to your own systems — is invisible to the platform and needs nothing
from it. An agent that reaches internal systems (MCP clients, intranet credentials) should be
hosted via `url:` so those clients and secrets live in your process, not the NPCI-shipped one.

---

## Configuration & secrets

Three homes, by responsibility:

| What | Where | Notes |
|------|-------|-------|
| Agent wiring (impl/url, prompt, model, timeouts, enabled) | `config/agents.yaml` | Non-secret; references secrets by env-var name only |
| Secrets (LLM keys, `SESSION_JWT_SECRET`, remote `AGENT_SERVICE_TOKEN`, `DATABASE_URL`) | env / `.env` | Never in the manifest or DB |
| Runtime operator settings (NPCI URLs, NPCI-issued API key / JWT / HMAC secrets) | `partner_settings` DB table | Edited from the Settings UI |

### LLM keys & model resolution

- **Keys** are read by provider from env (`PARTNER_ANTHROPIC_API_KEY`, `PARTNER_OPENAI_API_KEY`,
  `PARTNER_AINXT_API_KEY`), and may be overridden at runtime via the Settings UI
  (`partner_settings`). Precedence: **Settings-UI/DB → env**.
- **Provider + model** default from env (`LLM_PROVIDER`, `*_MODEL`, `AINXT_BASE_URL`), with an
  optional **per-agent `provider:`/`model:`** in `agents.yaml`. Precedence: **manifest → global**.
- In-process (`impl:`) agents use this config via `app/core/llm.py`. Remote (`url:`) agents own
  their keys/model in your service — the platform passes only input/output.

---

## Audit — `agent_runs`

Every `execute()` writes a row to `agent_runs` (partner-side mirror of NPCI's `agent_jobs`):
`agent_name, mode (local|http; mcp reserved), endpoint, status (running|succeeded|failed),
http_status, latency_ms, input_summary, result_payload, error_message`. A failed remote call or
a model that returns garbage becomes a visible row, not a swallowed exception.

---

## Prompts

In-process agent prompts live in `app/agents/prompts/<name>.md` and are read by
`app/agents/prompts.py:load_prompt(name, **vars)` (cached). Interpolation uses `string.Template`
(`$var` / `${var}`), which leaves literal `{ }` braces untouched — safe for prompts that embed
JSON. Edit the `.md`; no code change needed.

---

## Build your own agent (walkthrough)

1. **Edit a shipped agent in place.** Open `app/agents/design.py`, replace the body of `run()`
   with your logic, and edit `app/agents/prompts/design.md`. Restart. Done — the platform calls
   your code on the next change, and the run is audited.
2. **Add a brand-new agent.** Write `app/agents/myagent.py` with `class MyAgent(Agent)`, add a
   `prompts/myagent.md`, then register it in `config/agents.yaml`:
   ```yaml
   myagent: { impl: app.agents.myagent:MyAgent, prompt: myagent.md, enabled: true }
   ```
3. **Host it remotely instead.** Stand up a service honoring the HTTP contract above and change
   that agent's manifest entry to `url: https://your-host/agents/myagent`. No code change.

The platform wiring (executor, handlers, registry, audit) never changes in any of these.

---

## Scope of the automated code-review gate

The `code` agent's generation pipeline (`code/analyse` → `code/generate` →
`code/review` → `code/fix` → `code/merge-request`, see `docs/CODE_REVIEW_LOOP_PLAN.md`)
runs three review lenses over every generated file before a merge request can
be opened: two LLM reviewers (`code_reviewer` for correctness/quality,
`security_reviewer` for security defects) and one deterministic lint gate
(`agents/lint_gate.py` — a regex-based anti-pattern scan that can never miss a
pattern-matchable defect the way an LLM occasionally can). **"Any finding from
any of the three blocks the merge request"** is a hard, enforced gate — see
`api/dashboard/code.py::_review_status`.

Alongside the three blocking lenses there is one **advisory** lens: once a
review comes back clean, `agents/design_alignment.py` checks whether the
generated files actually match the implementation plan's stated intent —
"clean" only means the three blocking lenses found nothing, which is a
different claim from "this does what the design said." Its verdict is stored
as a `CodeReviewReport` row with `reviewer="design_alignment"` and shown in the
UI, but it carries an empty `findings` list **by design**: `_review_status()`
counts findings to decide whether to block, so a non-empty list there would
silently promote an advisory signal into a merge gate.

Two things this gate deliberately does **not** do, stated explicitly so the
scope boundary is discoverable rather than something you find out by reading
the code:

- **It does not generate or run tests.** A "clean" review means zero
  code-quality/security/lint findings — it is NOT evidence that the generated
  code compiles, runs, or passes any test. The opened merge request is
  explicitly a starting point for human review and must go through the
  partner's own CI/test suite before merging, exactly as any other
  contributor's MR would. (An optional, disabled-by-default test-generation
  agent exists — see `agents/test_files.py` — for partners who want automated
  test scaffolding as a supplementary signal; it is not part of the default
  gate and does not change this scope statement.)
- **It reviews ONLY what it itself generated, at the moment it generated it.**
  If a human edits the branch after the merge request is opened, those edits
  are NOT re-reviewed by this platform — they go through the partner's own
  GitLab review/CI process like any other commit on that branch.

A non-blocking, advisory design-alignment check (`agents/design_alignment.py`)
also runs once a review reaches "clean," comparing the generated files against
the implementation plan's stated intent and surfacing (via the job's progress
message) any deviations it finds — this is a signal for the human reviewer,
not a fourth gate; a "clean" review can still open a merge request even if
this check reports a deviation or cannot run at all (no LLM key, provider
error).

---

## Deployment

Standalone (no NPCI monorepo needed):

```bash
cd partner-platform

# Generate a self-signed dev TLS cert for the edge proxy (replace with a real
# cert for anything beyond local dev — see DEPLOYMENT_GUIDE.md §3.1/§10.2).
mkdir -p deploy/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/tls/key.pem -out deploy/tls/cert.pem -subj "/CN=localhost"

cp backend/.env.example backend/.env   # set SESSION_JWT_SECRET, PARTNER_SECRET_KEK; optionally an LLM key
docker compose up -d
```

Brings up Postgres + the FastAPI backend + the React frontend, all fronted by a
TLS-terminating `edge` nginx proxy (`:8443` HTTPS, `:8080` HTTP→HTTPS redirect
— the only container with a host-published port; see `deploy/edge.nginx.conf`).
On first login, open **Settings** and enter your NPCI Platform URL and
NPCI-issued credentials. With no LLM key, the feasibility agent returns mock
output so the stack still demonstrates end-to-end.

To load a real capability profile, mount your own file at `/app/data/partner_profile.md`
(the standalone compose ships `data/partner_profile.template.md`; a worked example is
`data/examples/hdfc_profile.md`), or set `PARTNER_PROFILE_PATH`.

### What changes on upgrade (from the pre-agent-framework version)

- The feasibility step now runs through the agent registry (adds an `agent_runs` row); its
  report shape is unchanged.
- The login session key moved to `SESSION_JWT_SECRET` → existing sessions invalidate once
  (users re-login); the app warns if the secret is left at its default.
- The capability profile is now a template; point `PARTNER_PROFILE_PATH` at your real profile.
- The NPCI↔partner A2A wire (Tier 1) is unchanged.
