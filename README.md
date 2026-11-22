# 🪐 Grafana AI Agent — Fullstack Observability Assistant

An intelligent, production-ready AI assistant that translates natural language into verified telemetry queries, constructs multi-stage diagnostics, provisions live Grafana dashboards and alert rules, and streams real-time cluster health metrics.

Built on **Google ADK**, **FastMCP (Model Context Protocol)**, **FastAPI**, **React 18 + Vite**, and the **SKILL.md v1.4 Observability Specification**.

Ships with **role-based access control** (OIDC identity → User & Role Store → server-side Policy Module → append-only audit log) and the **`agent-cli`** host-side administration client for adding users.

> **Looking for the admin instructions?** Jump to
> **[Adding a New User (Administrators)](#adding-a-new-user-administrators)**
> and **[The `agent-cli` Command Line Client](#the-agent-cli-command-line-client)**.

---

## ⚡ Quick Start (1-Command Run)

### 1. Configure Environment
```bash
cp .env.example .env
```
Open `.env` and insert your **`GEMINI_API_KEY`**.

### 2. Launch the Infrastructure (Docker Compose)
```bash
docker compose up -d --build
```
* **Prometheus** (scrapes the 4 simulated nodes `node-00`…`node-03`): `http://localhost:9090`
* **Grafana OSS**: `http://localhost:3000` (login `admin` / `admin`)
* **Loki**: `http://localhost:3100`
* **OpenSearch** (REST API): `http://localhost:9200`

The simulated nodes are reachable only inside the Docker network (container
port `9200`, not published to the host), so host port `9200` stays free for
OpenSearch. To stop the stack again: `docker compose down`.

> OpenSearch needs `vm.max_map_count >= 262144` on the host
> (`sudo sysctl -w vm.max_map_count=262144`); Prometheus, Grafana, Loki and
> the simulated nodes have no such requirement.

### 3. Launch the App
```bash
./start.sh
```
* **Backend**: `http://localhost:8008` (FastAPI)
* **Frontend UI**: `http://localhost:5173` (the vite dev server — the only
  normal application URL; it proxies `/api` to the backend on `:8008`)

👉 Open your browser to **`http://localhost:5173`**!

> The `agent` service in `docker-compose.yml` is the containerized
> alternative to `./start.sh`. It uses host networking so the `localhost`
> URLs in `.env` keep working, and listens on host port `8000` — while the
> local backend from `./start.sh` uses `8008`. Run one or the other, not both.

### 4. (Optional) Install the `agent-cli` Administration Client
```bash
pip install -e .
agent-cli --help
```
* The CLI adds accounts to the same User & Role Store the service uses — see
  **[The `agent-cli` Command Line Client](#the-agent-cli-command-line-client)**.

### 🛑 To Stop Everything
```bash
./stop.sh
```

---

## 🛠️ Manual Setup & Separate Terminals

If you prefer running backend and frontend in separate terminal windows:

### Prerequisites
* Python 3.10+ (with virtual environment)
* Node.js 18+ and npm
* Docker — for the infrastructure stack (`docker compose up -d --build`):
  Prometheus (`:9090`), Grafana (`:3000`), Loki (`:3100`), OpenSearch (`:9200`)

### Backend Setup (Terminal 1)
```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the API Gateway
python run_server.py
```
* Backend starts on `http://localhost:8008` with auto-reload.

### Frontend Setup (Terminal 2)
```bash
cd ui
npm install
npm run dev
```
* Frontend starts on `http://localhost:5173` with live HMR. It proxies `/api`
  to the FastAPI backend on `http://127.0.0.1:8008`, so the browser only ever
  talks to one origin — which is what the OIDC redirect URI
  (`http://localhost:5173/api/auth/callback`) expects.

---

## 🌟 Core Features & Operational Capabilities

### 1. Telemetry Querying & Node Comparisons
* **Zero Fabrication & Strict Label Discipline (Principle 9)**: Discovers actual runtime label values (`node_id`, `instance`) from Prometheus rather than guessing.
* **Instant vs. Range Queries**: Generates `instant` top-N evaluations or multi-node historical timeseries.
* **Interactive Charting**: Synchronized dual-line charts, categorical bar charts, and SVG radial gauges.

### 2. Multi-Stage Dependent Reasoning (SKILL.md v1.4)
* Supports chained queries where the second step depends on the output of the first (e.g. *"Which node has the highest CPU utilization right now, and how much memory is available on that node?"*).
* Executes Step 1 $\rightarrow$ extracts the winning entity $\rightarrow$ scopes Step 2 $\rightarrow$ synthesizes both results.

### 3. Live Dashboard Authoring (Grafana FastMCP)
* **Create from Scratch**: Compiles 2+ panel dashboards with automated grid layouts and PromQL expressions.
* **Update Live Dashboards**: Inserts gauges, stats, and logs into existing Grafana dashboards.
* **Interactive Proposals**: Generates proposal cards with live chart previews and JSON diffs before applying changes to Grafana.

### 4. Alert Rule Creation & Provisioning (§12)
* Translates threshold requests into formal Prometheus alert rules (`condition_query`, `operator`, `threshold`, `for_duration`).
* **Human-in-the-Loop Confirmation**: Click **"Confirm / Create Alert"** in the UI to provision directly into Grafana with a live deep link.

### 5. Real-Time Metrics Rail
* Real-time sidebar sparklines monitoring **CPU Busy %**, **Memory Available**, **GPU Temperature**, and **GPU Utilization**.
* 1-click collapse (`✕`) to expand the chat interface to 100% full screen.

---

## 🎬 Tested Demo Prompts

| Feature Area | Prompt | Expected Outcome |
|---|---|---|
| **Multi-Node Comparison** | `plot cpu utilization for node-00 and node-01 over the last hour` | Synchronized side-by-side timeseries chart |
| **Root-Cause Triage (Chained)** | `Which node has the highest CPU utilization right now, and how much memory is available on that node?` | Discovers top node $\rightarrow$ queries memory $\rightarrow$ returns 2-in-1 synthesis |
| **Create Dashboard** | `create a dashboard called Fleet Overview with node_cpu_seconds_total and node_memory_MemAvailable_bytes` | Generates 2-panel dashboard proposal card with "Apply Changes" button |
| **Update Dashboard** | `add a DCGM_FI_DEV_GPU_TEMP gauge to dashboard uid observability-overview` | Appends GPU gauge to live Grafana dashboard |
| **Alert Rule Provisioning** | `alert me if CPU utilization exceeds 90% on node-00 for 5 minutes` | Proposes alert $\rightarrow$ Confirm button creates live Grafana rule |
| **Log Diagnostics** | `show error logs for container log-generator-ai-lab over the last 15 minutes` | Queries Loki and streams structured error events |

---

## 🧪 Testing & Verification

### Run Backend Unit Tests
```bash
pytest tests/
```
* The suite is deterministic and has no external dependencies. The current
  run is **1015 passed, 3 skipped, 6 failed** — the six failures are all in
  `tests/test_concept_catalog.py` and are pre-existing (dashboard concept
  mapping), unrelated to the RBAC work.

### Test the CLI
```bash
pytest tests/test_cli_users_add.py
```
* Covers the argument validation, the duplicate check, the audit records, the
  `authz_version` bump and the administrative guardrails.

### Build Frontend Production Bundle
```bash
cd ui && npm run build
```
* Compiles TypeScript and bundles Vite assets with 0 errors.

---

## 📁 Repository Structure

```text
├── start.sh                 # 1-click startup script (Backend + Frontend)
├── stop.sh                  # 1-click stop script
├── run_server.py            # Main entrypoint launching the unified API Gateway
├── pyproject.toml           # Package definition of the `agent-cli` console script
├── .env.example             # Complete environment variables template
├── app/
│   ├── api/
│   │   ├── main.py          # FastAPI application & router registry
│   │   ├── routes_chat.py   # Unified chat gateway & multi-turn coordinator
│   │   ├── routes_query.py  # Telemetry query endpoint (/api/v1/query)
│   │   ├── routes_proposals.py # Dashboard proposal lifecycle
│   │   ├── routes_admin.py  # Administrative endpoints (users, scopes, audit log)
│   │   └── routes_auth.py   # OIDC login, callback, whoami, logout
│   ├── auth/
│   │   ├── policy.py        # Central Policy Module: roles, scopes, impact classes
│   │   ├── principal.py     # The verified identity (issuer, subject, role, scopes)
│   │   ├── dependencies.py  # Session boundary + require_principal / require_admin_principal
│   │   ├── resource_scope.py# Dashboard / folder scope enforcement
│   │   ├── environment.py   # Environment scope resolution & catalogue
│   │   └── audit.py         # Audit event vocabulary & recorder
│   ├── storage/
│   │   ├── database.py      # Engine & session factory for the User & Role Store
│   │   ├── models.py        # users / user_scopes / audit_log / proposals tables
│   │   ├── user_store.py    # User & Role Store repository + admin guardrails
│   │   ├── audit_store.py   # Append-only audit log
│   │   └── seed.py          # Idempotent demo data seeding
│   ├── pipeline.py          # Router -> Generator -> Validator -> Executor pipeline
│   ├── label_discovery.py   # Live Prometheus runtime label harvester
│   ├── validator.py         # Deterministic schema & PromQL validator
│   ├── catalog.json         # Dashboard UI catalog (served by /api/catalog)
│   ├── catalog/             # Structured Prometheus metric catalog (catalog.json + retrieval/runtime)
│   ├── executor.py          # Prometheus / Loki / OpenSearch execution engine
│   └── grafana_tools/       # FastMCP Grafana dashboard authoring & compilation
├── cli/                     # `agent-cli` — host-side administration client
│   ├── main.py              # argparse front end (admin -> users -> add)
│   └── users.py             # Validation + the add_user() business operation
├── skills/
│   ├── SKILL.md             # Observability Query Builder specification (v1.4)
│   └── references/          # Domain reference guides (Node Exporter, DCGM, PromQL)
└── ui/
    ├── src/
    │   ├── App.tsx          # Main application shell & tab navigation
    │   ├── api.ts           # Unified frontend API client
    │   └── components/
    │       ├── AdkAssistantTab.tsx   # Conversational chat UI & metrics rail
    │       ├── ProposalCard.tsx      # Interactive Grafana proposal card
    │       ├── UserManagementModal.tsx # Admin UI: users, roles, scopes, pending deletions
    │       └── TimeSeriesChart.tsx   # Recharts visualization engine (Line, Gauge, Bar)
    └── package.json
```

---

## 🔧 Configuration Reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini_api` | `gemini_api` (AI Studio key) or `vertex` (explicit opt-in) |
| `GEMINI_API_KEY` | *(Required for `gemini_api`)* | Gemini Developer API key; never sent to Vertex AI |
| `VERTEX_PROJECT` / `VERTEX_LOCATION` | *(none)* / `global` | Vertex AI project (ADC auth); only with `LLM_PROVIDER=vertex` |
| `VERTEX_API_KEY` | *(none)* | Vertex express-mode key (alternative to `VERTEX_PROJECT`) |
| `GEMINI_MODEL` | `gemini-3.7-flash` | Model used for Router and Generator passes (and for the ADK agent — both paths use the same setting) |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus HTTP endpoint |
| `LOKI_URL` | `http://localhost:3100` | Loki HTTP endpoint |
| `OPENSEARCH_URL` | `http://localhost:9200` | OpenSearch REST endpoint (HTTP, not the legacy transport port) |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana OSS endpoint |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | `glsa_...` | Grafana Service Account token with Admin/Editor permissions |
| `ALERT_RULE_CREATION_ENABLED` | `true` | Enables alert rule proposal and provisioning (§12) |
| `DEPENDENT_QUERY_RESOLUTION_ENABLED` | `true` | Enables multi-stage chained query resolution (v1.4) |
| `PIPELINE_TIMEOUT_SECONDS` | `45.0` | Timeout ceiling for LLM and backend query resolution |
| `CATALOG_PATH` | `app/catalog/catalog.json` | Structured Prometheus metric catalog (**not** the dashboard catalog `app/catalog.json`) |
| `CATALOG_NARROW_MIN_SCORE` | `2.0` | Minimum catalog search score before ranked candidates are shown to the Generator |
| `CATALOG_METRIC_STATUS_VALIDATION_ENABLED` | `false` | Reject metrics whose catalog status is `discovered_pending_review` / `rejected` |
| `AUTHENTIK_ENABLED` | `false` | Require an authenticated identity on the protected API endpoints (**development mode when `false`**) |
| `AUTHENTIK_BASE_URL` | `http://localhost:9000` | Browser-visible base URL of the Authentik instance; used to build the enrollment and password-change links |
| `AUTHENTIK_APP_SLUG` | `grafana-ai-agent` | Slug of the Authentik application the OIDC provider belongs to |
| `AUTHENTIK_OIDC_DISCOVERY_URL` | `http://localhost:9000/application/o/grafana-ai-agent/.well-known/openid-configuration` | OIDC discovery document of the Authentik provider |
| `AUTHENTIK_OIDC_CLIENT_ID` | *(none)* | OIDC client id of the Authentik application; required when `AUTHENTIK_ENABLED=true` |
| `AUTHENTIK_OIDC_CLIENT_SECRET` | *(none)* | OIDC client secret (server-side only); required when `AUTHENTIK_ENABLED=true` |
| `AUTHENTIK_OIDC_REDIRECT_URI` | `http://localhost:5173/api/auth/callback` | Exact redirect URI registered in the Authentik provider |
| `AUTHENTIK_POST_LOGOUT_REDIRECT_URI` | `http://localhost:5173/` | Exact "Strict Post Logout URI" registered in the Authentik provider; where the user lands after the sign-out |
| `AUTHENTIK_OIDC_SCOPE` | `openid profile email` | Scopes requested in the authorization request |
| `AUTHENTIK_OIDC_PKCE` | `true` | Use PKCE (S256) in the authorization request |
| `AUTHENTIK_SESSION_SECRET` | *(none)* | Secret that signs the application-session cookie; required when `AUTHENTIK_ENABLED=true` |
| `AUTHENTIK_SESSION_MAX_AGE_SECONDS` | `3600` | Lifetime of the application session |
| `AUTHENTIK_SESSION_COOKIE_SECURE` | `false` | `Secure` flag of the session cookie (set to `true` behind HTTPS) |
| `AUTHENTIK_ENROLLMENT_FLOW` | `default-enrollment-flow` | Slug of the Authentik **enrollment** flow the *Create account* link opens |
| `AUTHENTIK_PASSWORD_CHANGE_FLOW` | `default-password-change` | Slug of the Authentik flow the *Change password* action opens |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/grafana_ai` | Connection string of the **User & Role Store** (RBAC). Falls back to local SQLite at `sqlite:///./app_data/rbac.db` when unset or unreachable |
| `DATABASE_AUTO_SEED` | `true` | Seed the demo accounts, proposals and audit records into an empty User & Role Store. Idempotent and never overwrites an existing record |

---

## 📚 Structured Metric Catalog

There are **two different catalogs** in this repo — do not confuse them:

| File | Used for |
|---|---|
| `app/catalog.json` | Dashboard UI catalog served by `GET /api/catalog` (unchanged) |
| `app/catalog/catalog.json` | Structured Prometheus metric catalog (43 metrics: 16 Node Exporter, 27 DCGM) used by the query pipeline |

`app/grafana_tools/concept_catalog.py` is a third, separate thing (dashboard concept mapping).

How the structured catalog is used (`app/catalog/runtime.py`, wired into `app/pipeline.py`):

* **Candidate retrieval.** For each request — and for each dependency stage separately — a deterministic ranked search over the question suggests metrics. They are intersected with the metrics of the references the Router matched for *that* request/stage, and shown to the Generator as an advisory block.
* **Authority is unchanged.** Markdown references still define metric semantics; live Prometheus label discovery still defines which label keys exist; the deterministic validator still checks every metric against the opened references.
* **Fail-safe.** A catalog miss, a low-confidence hit (below `CATALOG_NARROW_MIN_SCORE`), or a missing/malformed `catalog.json` all fall back to the previous behavior. "Not in the catalog" never means "unsupported".
* **Optional status validation** (`CATALOG_METRIC_STATUS_VALIDATION_ENABLED`, off by default) additionally rejects `discovered_pending_review` / `rejected` metrics for both query and alert-rule results.
* **Lifecycle.** Loaded once (warmed at startup, otherwise lazily) and cached. `POST /api/v1/admin/reload-skill` (and `root_agent.reload_skill()`) also reloads it; a bad edit keeps the previous good catalog.

Measure retrieval quality with `python -m scripts.evaluate_catalog_retrieval`.

---

## 🔐 Authentication (Authentik, OpenID Connect)

Authentication is **OpenID Connect** against Authentik. The browser
authenticates on Authentik's own login form; the backend is the OIDC
relying party: it sends the browser to Authentik, validates the returned ID
token and keeps the session in a signed, HttpOnly cookie.

```
browser ──▶ vite dev server (:5173)  ──/api──▶ FastAPI backend (:8008)
   │                                              │
   └──▶ Authentik (:9000) ◀── Authorization Code flow ──┘
```

There is **no reverse proxy** in front of the application any more: the vite
dev server on `:5173` serves the UI *and* proxies `/api` to the backend on
`:8008`, so the browser only ever talks to one origin. The OIDC callback
(`http://localhost:5173/api/auth/callback`) is therefore served by the
backend through that very proxy.

What the backend does with the provider's answer:

* the authorization request carries a fresh `state`, a fresh `nonce` and a
  PKCE challenge (S256), and no `prompt=login` — that would make Authentik
  run its authentication flow twice and ask for the credentials twice;
* the callback rejects a `state` that does not match the login attempt it
  belongs to;
* the authorization code is exchanged for tokens at the provider's token
  endpoint (the client secret never leaves the server);
* the ID token's **signature** is verified against the provider's JWKS, and
  its `iss`, `aud`, `exp` and `nonce` claims are checked;
* only then is a session established — a signed, HttpOnly cookie holding
  the stable identity (`sub`, `iss`) and the profile fields the UI shows.
  No access token, refresh token or ID token is stored in it, and none of
  them ever reaches the browser. The ID token itself is kept on the server
  (in memory) because the sign-out needs it as the `id_token_hint`.

> Full step-by-step setup, including the Authentik admin configuration:
> **[AUTHENTIK_SETUP_GUIDE.md](AUTHENTIK_SETUP_GUIDE.md)**.

| File | Purpose |
|---|---|
| `app/auth/oidc.py` | The OIDC client: discovery, authorization request, token exchange, ID-token verification |
| `app/auth/session.py` | The signed, HttpOnly application-session cookie |
| `app/auth/id_token_store.py` | Server-side retention of the ID token for the RP-initiated logout |
| `app/auth/dependencies.py` | The FastAPI dependency: session boundary + principal |
| `app/auth/principal.py` | The verified identity (`issuer`, `subject`, profile fields) |
| `app/api/routes_auth.py` | `login`, `callback`, `whoami`, `config` and `logout` |

### What is public and what is protected

| Endpoint | Access |
|---|---|
| `/healthz`, `/readyz`, `/api/health` | Public (liveness/readiness) |
| `/api/auth/config` | Public — the login screen's configuration (login path, sign-up flow URL). No secret |
| `/api/auth/login`, `/api/auth/callback` | Public — they *are* the login |
| `/` and the UI assets | Public — so that the branded login screen can render for a visitor without a session |
| `/api/auth/whoami`, `/api/auth/logout` | Require an authenticated identity |
| `/api/v1/capabilities` | Requires an authenticated identity |
| `/api/chat`, `/api/query`, `/api/v1/query`, `/api/catalog`, `/api/labels`, `/api/search`, `/api/proposals*`, `/api/conversations*`, `/api/alerts*`, `/api/alert-proposals*`, `/api/v1/alerts/confirm`, `/api/adk/*` | Requires an authenticated identity |
| `/api/v1/admin/*` | Requires an authenticated **administrator** (role `admin`) — see [Authorization (RBAC)](#authorization-rbac) |

An unauthenticated API call is answered with `401`, never with data. No
identity header sent by the browser is ever believed.

The identity is published on `request.state.principal` (and returned by
`/api/auth/whoami`) as the stable `(issuer, subject)` pair, and the
authorization layer described in [Authorization (RBAC)](#authorization-rbac)
builds on it without changing authentication. Authentik's group and
entitlement claims are **not** read by the application: Authentik verifies
identity, it does not hand the application its privileges — the role, the
status and the scopes come from the application's own User & Role Store.

### Signing in, signing up and signing out

* **Login** — the login screen's *Login with Authentik* button goes to
  `/api/auth/login`, which starts the Authorization Code flow. Authentik
  shows its own login form; the application never sees a password.
* **Create account** — the login screen's *Create account* link opens
  Authentik's own **enrollment** flow, which asks for the email address and
  the password and creates the user. There is no registration API in this
  application.
* **Change password** — the profile menu opens Authentik's password-change
  flow. The password is entered on the Authentik server.
* **Sign out** — the profile menu calls `/api/auth/logout`, which clears
  *this application's* session and answers with the provider's
  RP-initiated logout URL (`client_id`, `id_token_hint` and the
  `post_logout_redirect_uri`). The browser follows it: Authentik ends the
  SSO session and returns the user to the login screen, so the next
  sign-in asks for the credentials again.

### Local startup with authentication

```bash
# 1. Configure (.env) -- see AUTHENTIK_SETUP_GUIDE.md for the exact values
AUTHENTIK_ENABLED=true
AUTHENTIK_OIDC_CLIENT_ID=<from the Authentik admin interface>
AUTHENTIK_OIDC_CLIENT_SECRET=<from the Authentik admin interface>
AUTHENTIK_SESSION_SECRET=<openssl rand -hex 32>

# 2. Start Authentik in its own compose project (separate from this repo)
cd /path/to/authentik && docker compose up -d

# 3. Start the backend and the frontend (as usual)
./start.sh

# 4. Open the application
open http://localhost:5173
```

### Development mode

With `AUTHENTIK_ENABLED=false` the API is completely open and no identity is
established. This keeps the local workflow usable while the Authentik
provider is being set up. It is a development convenience, **not** a
security control: the backend logs a warning at startup, and the setting
must be flipped to `true` before the service is reachable by anyone else.

### Authorization (RBAC)

A successful login establishes *identity only*. **Authorization is a separate,
server-side layer** built on top of it: the **User & Role Store**, the
**Central Policy Module** and the **Audit Log**.

The design is specified in **[RBAC_Grafana_ADK_Final (1).md](RBAC_Grafana_ADK_Final%20(1).md)**;
the implementation lives in `app/auth/` (policy, scopes, environments) and
`app/storage/` (the store itself).

```
Authentik  ──identity (issuer, subject)──▶  User & Role Store
                                                 │  role, status, scopes
                                                 ▼
                                          Principal  ──▶  Policy Module
                                                                │
                                            allow / deny ──────┴──▶  Audit Log
```

#### The User & Role Store

The store is a relational table set (PostgreSQL in the compose stack, SQLite
at `app_data/rbac.db` as a fallback) holding three things:

| Table | Holds |
|---|---|
| `users` | `user_id`, `issuer`, `subject`, `email`, `username`, `display_name`, `status`, `role`, `authz_version`, `created_at`, `last_seen_at` |
| `user_scopes` | The four scope lists per user: `allowed_folder_uids`, `allowed_dashboard_uids`, `denied_dashboard_uids`, `allowed_environments`, plus `role_grants` |
| `proposals` / `approvals` | Dashboard and alert proposals, their impact class, their state and the approval tokens |
| `audit_logs` | Append-only record of every identity, role, scope and proposal event |

The `(issuer, subject)` pair is the stable identity key — the same key Authentik
asserts. The email address and the login name are **profile fields only**;
they never identify anybody.

#### Roles

Four application roles, defined once in `app/auth/policy.py` (`VALID_ROLES`):

| Role | Dashboards & panels | Alert rules | PromQL / queries | Administration |
|---|---|---|---|---|
| **admin** | create, edit, approve, execute | propose, approve, execute | allowed (bounded) | full — role, status and scope management |
| **maintainer** | create, edit, approve, execute | — (denied) | allowed | — |
| **operator** | — (denied) | propose, approve, execute | allowed | — |
| **observer** | read-only (dashboards & catalog) | — (denied) | **denied** | — |

The boundaries are enforced in `app/auth/policy.py`, not in the UI:

* `authorize_dashboard_read` / `authorize_dashboard_propose` / `authorize_dashboard_approve` / `authorize_dashboard_execute`
* `authorize_alert_propose` / `authorize_alert_approve` / `authorize_alert_execute`
* `authorize_promql` — observers may not execute Prometheus queries at all
* `authorize_proposal_rejection`, `authorize_alert_rejection`

Every decision returns a `PolicyDecision` with an `allowed` flag, a
human-readable `reason`, an `impact_class` and a `requires_second_approver`
flag. A denial is a `403` with the reason attached.

#### Account statuses

Three statuses (`VALID_STATUSES`): `active`, `pending`, `deactivated`.

* An **unknown identity that signs in for the first time** is auto-registered
  as `pending`, with **no role and no scopes** (`UserStore.create_pending_user`,
  `app/auth/dependencies.py`). It can authenticate, but every protected
  endpoint answers `403 — Account pending activation by an administrator.`
  until an administrator activates it.
* `deactivated` accounts are refused the same way.
* A `pending` account may also be **linked** to a real identity on first
  sign-in: if a pre-seeded record carries the placeholder subject prefix
  `ak_sub_` and the sign-in matches on email or username, the record is
  re-keyed to the real `(issuer, subject)` instead of creating a duplicate.

#### Resource scope

Every account carries four scope lists, evaluated by `is_dashboard_in_scope`
(`app/auth/policy.py`) with this precedence:

1. **Explicit deny** — a UID in `denied_dashboard_uids` is refused, and the
   deny wins over every allowance that the scope evaluation would otherwise
   grant (including for an administrator, on the read path).
2. **Role grants** — `role_grants` tie a *role* to specific folders and
   dashboards, so one account can be a maintainer in one folder and an
   observer in another. `Principal.get_role_for_folder()` /
   `get_role_for_dashboard()` resolve the effective role per resource.
3. **Allowed dashboards** — `allowed_dashboard_uids` grants specific dashboards.
4. **Allowed folders** — `allowed_folder_uids` grants a whole folder; `*`
   grants every folder.

Anything unknown **fails closed**: a missing dashboard UID or folder UID never
grants access on its own.

An **administrator** keeps global access. The folder and dashboard scope
checks are not applied to an administrator's mutations
(`authorize_dashboard_propose` / `authorize_dashboard_execute`), and the
environment check is bypassed for the admin role.

The **environment** is the fourth scope dimension
(`allowed_environments`, `app/auth/environment.py`). A requested environment
is validated against the deployment catalogue (server-side configuration plus
the environments found in the store) **and** against the principal's own
permitted list. Administrators bypass the environment check. The environment
is resolved *before* Prometheus or Grafana is touched, so a rejected operation
never reaches the query executor or a Grafana write.

#### Separation of duties and impact classification

A proposal's impact class is **recomputed server-side from its own operation**
(`classify_impact`), never trusted from a client-supplied field. A payload
that downgrades a destructive operation still gets the destructive
classification.

* `routine_edit` — one approver is enough.
* `destructive_delete` — a **second, independent approver** is required: a
  proposer may never approve their own destructive change, and never reject
  their own proposal either.

`revalidate_proposal_for_execution()` re-checks the approval state, the impact
classification, the second-approver requirement and the approver's active
status **immediately before execution** — an approval granted earlier is
never trusted on its own.

#### The Audit Log

Every identity event, role change, status change, scope change, proposal
transition and administrative refusal is recorded in the append-only
`audit_logs` table. The change and its audit record are written **in the same
transaction**: if the audit records cannot be written, the change does not
happen. Sensitive values are redacted before they are stored.

Recorded event types include `first_sign_in`, `user_role_changed`,
`user_status_changed`, `user_scope_changed`, `user_access_modified`,
`proposal_created`, `proposal_modified`, `proposal_approved`,
`proposal_rejected`, `proposal_executed`, `proposal_execution_failed`,
`proposal_access_denied`, `alert_proposed`, `alert_approved`,
`alert_rejected`, `alert_executed`, `alert_execution_failed`,
`dashboard_created`, `dashboard_updated`, `dashboard_deleted`,
`panel_created`, `panel_updated`, `panel_deleted`,
`dashboard_access_denied`, `scope_violation`, `authentication_failed`,
`authentication_succeeded`, `logout`, `admin_users_listed`,
`admin_folders_listed`, `admin_access_denied`, `admin_audit_log_viewed`,
`admin_seed_audit_viewed`, `admin_skill_reloaded` and
`admin_skill_reload_failed`.

#### Administrative endpoints

All of these require the **admin** role, checked server-side by the
`require_admin_principal` dependency (`app/auth/dependencies.py`) — hiding a
button in the UI is not a control.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/admin/users` | List every account with its role, status, `authz_version` and scopes |
| `GET /api/v1/admin/folders` | The Grafana folder inventory, used to assign folder scopes |
| `PUT /api/v1/admin/users/{user_id}/access` | Set a user's role, status and scopes |
| `GET /api/v1/admin/audit-log` | Read the audit log |
| `GET /api/v1/admin/seed-audit` | Report demo accounts whose Grafana UID references are stale (it only *reports*; it never repairs a reference) |
| `GET /api/v1/admin/pending-deletions` | List destructive proposals awaiting an administrator decision |
| `POST /api/v1/admin/pending-deletions/{proposal_id}/approve` \| `/reject` | Approve or reject a destructive proposal |
| `POST /api/v1/admin/reload-skill` | Re-parse `SKILL.md` without restarting the service |

The `PUT .../access` endpoint applies the same guardrails as the CLI:

* **No self-privilege change** — an administrator cannot modify their own
  role, status or scopes.
* **Last-administrator protection** — the last active administrator cannot
  be demoted or deactivated.
* Every change bumps the target's `authz_version` counter, which is how an
  open session learns that its permissions changed.

#### Development mode

With `AUTHENTIK_ENABLED=false` the API is completely open: no identity is
established and every caller is granted the unrestricted development
principal. This is described under
[Development mode](#development-mode) above — it is a local convenience,
never a security control.

---

## Adding a New User (Administrators)

There are **two** ways to give a person access, and they write to the same
store through the same application logic.

### The identity comes from Authentik first

The application never creates passwords. A person becomes a *known* identity
by signing in through Authentik's own **enrollment flow** — the *Create
account* link on the login screen — which asks for the email address and the
password **on the Authentik server**. The application only ever sees the
resulting `(issuer, subject)` pair.

The first time that identity signs in, it is auto-registered as **`pending`**
with no role and no scopes. Until an administrator activates it, every
protected endpoint answers `403 — Account pending activation by an
administrator.`

### Option 1 — the Admin UI (interactive)

1. Sign in as an **administrator** (the profile menu shows the admin badge).
2. Open the profile menu and click **🛡️ Admin Console & Users** (the *User
   Management* modal). A **🗑️ Pending Deletions** button appears next to it
   when destructive proposals are waiting.
3. Find the account in the list — a `pending` account is shown with a
   *pending* badge and no role.
4. Set the **role** and the **status** (`active`) and edit the **scopes**:
   the allowed folders (picked from the live Grafana folder list), the
   allowed dashboard UIDs, the permitted environments, and the per-folder
   **role grants**. (The UI preserves the existing *denied* dashboard list; it
   does not edit it.)
5. Save. The change is audited against you and the account's
   `authz_version` is bumped immediately.

The same modal has a **Pending Deletions** tab where an administrator approves
or rejects destructive proposals.

### Option 2 — the `agent-cli` (scriptable)

`agent-cli admin users add` is the **host-side management command** for the
same store. It runs on the host, against the same database the service uses,
and applies the application's own user-creation and user-management logic — so
the audit log, the `authz_version` counter and the administrative guardrails
are exactly the ones the service enforces.

#### Installation

The CLI ships as the `agent-cli` console script of this repo:

```bash
# from the repository root, inside the project's virtual environment
pip install -e .
agent-cli --help
```

It reads the same `.env` as the service, so `DATABASE_URL` must point at the
same User & Role Store. Because the `.env` path is relative, run the command
**from the repository root**. To run it against another database, override
the variable for the single invocation:

```bash
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/grafana_ai" \
  agent-cli admin users add ...
```

#### Usage

```bash
agent-cli admin users add --subject <subject> [options]
```

| Flag | Meaning |
|---|---|
| `--subject` | **Required.** The Authentik subject of the account (`sub` from the ID token). Must not contain whitespace |
| `--issuer` | The Authentik issuer. Defaults to the configured one (`http://localhost:9000/application/o/grafana-ai-agent/`) |
| `--username` | Login name — a profile field, it never identifies anybody |
| `--name` | Display name — a profile field |
| `--email` | Email address — informational only |
| `--admin` | The administrator the change is **audited against**. Required whenever a role, a status other than `pending` or a scope is assigned; the referenced account must exist and be an *active administrator* |
| `--role` | One of `admin`, `maintainer`, `operator`, `observer`. Assigning a role activates the account |
| `--status` | One of `active`, `pending`, `deactivated`. Default: `pending` |
| `--allow-folder` | A Grafana folder UID the account may use. Repeatable; one UID per flag |
| `--allow-dashboard` | A Grafana dashboard UID the account may use. Repeatable |
| `--deny-dashboard` | A Grafana dashboard UID the account may **never** use. Repeatable |
| `--allow-env` | An environment name the account may query. Repeatable |
| `--json` | Print the created account as JSON |

`--allow-folder`, `--allow-dashboard` and `--allow-env` also accept the
wildcard `*`.

#### Examples

```bash
# 1. Create a pending account (no role, no scopes) — the account can sign in
#    but has no privileges until an administrator activates it.
agent-cli admin users add --subject 6f1c9b04-2a7e-4c31-9d55-1b0f8e7a3d21 \
    --username morgan.lee --name "Morgan Lee (New Hire)" --email newhire@example.com

# 2. Create an active maintainer with a folder scope, audited against an
#    administrator.
agent-cli admin users add --subject b3d5e8f2-77c4-4a19-b0e6-9c2a5d4f6e83 \
    --username alex.rivera --name "Alex Rivera (Maintainer)" --email maintainer@example.com \
    --admin usr_admin_01 --role maintainer --allow-folder fld-payments \
    --allow-dashboard dash-checkout-slo --allow-env staging

# 3. Create an operator for the alerting folder only.
agent-cli admin users add --subject c9a1d4b7-3e58-4f26-8c0d-7b2e9a5f1c64 \
    --username sam.chen --name "Sam Chen (On-Call SRE)" --email operator@example.com \
    --admin usr_admin_01 --role operator --allow-folder fld-alerts \
    --allow-env staging --allow-env production

# 4. Machine-readable output for a provisioning script.
agent-cli admin users add --subject d5e7f9a1-4b6c-4d38-9e2f-8c3a1b7d5e90 \
    --username alice --admin usr_admin_01 --role observer --json
```

Example output:

```text
[200 OK] Created user usr_342d9df539e0 status=active role=maintainer authz_version=2
        Folders:       fld-payments
        Dashboards:    dash-checkout-slo
        Denied:        -
        Environments:  staging
```

#### Exit codes

| Code | Meaning |
|---|---|
| `0` | The account was created |
| `1` | Invalid input or a refused administrative change (e.g. `--admin` missing, the administrator is not active, a self-privilege change, the last active administrator) |
| `3` | An account with the same `(issuer, subject)` already exists |

#### Notes and limitations

* **The subject is the Authentik `sub`** — the `sub` claim of the person's ID
  token, a UUID in a stock Authentik deployment. The subject of a *pre-seeded*
  demo record is the placeholder `ak_sub_…`; when such a record signs in for
  the first time it is linked to the real identity by email or username.
  Creating a *new* record with a placeholder subject gives you a second,
  separate account.
* **How to find the subject.** Sign the person in once and read
  `GET /api/auth/whoami` — it reports the `subject` of the current session.
  Before that, an administrator can read it from the Authentik admin
  interface.
* **Scope flags are one value per flag.** `--allow-folder a,b` is rejected —
  repeat the flag instead.
* **The CLI does not create Authentik accounts and does not set passwords.**
  The person still has to sign in through Authentik once.
* The CLI cannot change an *existing* account; use the Admin UI
  (`PUT /api/v1/admin/users/{user_id}/access`) for role, status and scope
  changes on accounts that are already there.

### Then verify the new user

1. The person signs in at `http://localhost:5173` with **Create account** (if
   they do not have an Authentik account yet) and then **Login with Authentik**.
2. The first sign-in creates the `pending` account (or links the one you
   created with the CLI, if the identity matches).
3. `GET /api/auth/whoami` reports the resolved `role`, `status` and `scopes`
   for that session.
4. An administrator can inspect the change in `GET /api/v1/admin/audit-log`.

---

## The `agent-cli` Command Line Client

The CLI is deliberately **not** a subcommand of the user-facing API client. It
is a host-side management tool that talks to the User & Role Store directly:

```
cli/
├── __main__.py     # python -m cli
├── main.py         # argparse front end -> the `agent-cli` console script
└── users.py        # validation + the add_user() business operation
```

* `cli/main.py` builds the argument parser (`admin` → `users` → `add`) and
  maps the outcome onto the exit codes above.
* `cli/users.py` validates every argument, then delegates to
  `UserStore.create_pending_user()` and `UserStore.update_user_access()` —
  the very same store methods the Admin UI endpoint calls. The CLI contains
  no privileged path of its own: it cannot bypass the guardrails, because it
  does not implement them.
* The command never touches Grafana directly. Folder and dashboard UIDs are
  validated for syntax; whether they exist in Grafana is checked by the Admin
  UI's folder picker and reported by `GET /api/v1/admin/seed-audit`.
