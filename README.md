# 🪐 Grafana AI Agent — Fullstack Observability Assistant

An intelligent, production-ready AI assistant that translates natural language into verified telemetry queries, constructs multi-stage diagnostics, provisions live Grafana dashboards and alert rules, and streams real-time cluster health metrics.

Built on **Google ADK**, **FastMCP (Model Context Protocol)**, **FastAPI**, **React 18 + Vite**, and the **SKILL.md v1.4 Observability Specification**.

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

### Run Backend Unit Tests (205 Tests)
```bash
pytest tests/
```
* All 205 tests are deterministic and run in under 3 seconds with zero external dependencies.

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
├── .env.example             # Complete environment variables template
├── app/
│   ├── api/
│   │   ├── main.py          # FastAPI application & router registry
│   │   ├── routes_chat.py   # Unified chat gateway & multi-turn coordinator
│   │   ├── routes_query.py  # Telemetry query endpoint (/api/v1/query)
│   │   └── routes_proposals.py # Dashboard proposal lifecycle
│   ├── pipeline.py          # Router -> Generator -> Validator -> Executor pipeline
│   ├── label_discovery.py   # Live Prometheus runtime label harvester
│   ├── validator.py         # Deterministic schema & PromQL validator
│   ├── catalog.json         # Dashboard UI catalog (served by /api/catalog)
│   ├── catalog/             # Structured Prometheus metric catalog (catalog.json + retrieval/runtime)
│   ├── executor.py          # Prometheus / Loki / OpenSearch execution engine
│   └── grafana_tools/       # FastMCP Grafana dashboard authoring & compilation
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
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model used for Router and Generator passes |
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
| `AUTHENTIK_OIDC_SCOPE` | `openid profile email` | Scopes requested in the authorization request |
| `AUTHENTIK_OIDC_PKCE` | `true` | Use PKCE (S256) in the authorization request |
| `AUTHENTIK_SESSION_SECRET` | *(none)* | Secret that signs the application-session cookie; required when `AUTHENTIK_ENABLED=true` |
| `AUTHENTIK_SESSION_MAX_AGE_SECONDS` | `3600` | Lifetime of the application session |
| `AUTHENTIK_SESSION_COOKIE_SECURE` | `false` | `Secure` flag of the session cookie (set to `true` behind HTTPS) |
| `AUTHENTIK_ENROLLMENT_FLOW` | `default-enrollment-flow` | Slug of the Authentik **enrollment** flow the *Create account* link opens |
| `AUTHENTIK_PASSWORD_CHANGE_FLOW` | `default-password-change` | Slug of the Authentik flow the *Change password* action opens |

> **Known issue (deferred):** model-name defaults are inconsistent. `Settings.gemini_model` defaults to
> `gemini-3.5-flash-lite`, `.env.example` sets `GEMINI_MODEL=gemini-3.7-flash`, and the ADK agent
> (`app/adk_agent.py`) silently remaps `gemini-3.7-flash` to `gemini-2.5-flash`, so the ADK path and the
> direct Router/Generator client can run different models. To be resolved separately.

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
  PKCE challenge (S256);
* the callback rejects a `state` that does not match the login attempt it
  belongs to;
* the authorization code is exchanged for tokens at the provider's token
  endpoint (the client secret never leaves the server);
* the ID token's **signature** is verified against the provider's JWKS, and
  its `iss`, `aud`, `exp` and `nonce` claims are checked;
* only then is a session established — a signed, HttpOnly cookie holding
  the stable identity (`sub`, `iss`) and the profile fields the UI shows.
  No access token, refresh token or ID token is stored in it, and none of
  them ever reaches the browser.

> Full step-by-step setup, including the Authentik admin configuration:
> **[AUTHENTIK_SETUP_GUIDE.md](AUTHENTIK_SETUP_GUIDE.md)**.

| File | Purpose |
|---|---|
| `app/auth/oidc.py` | The OIDC client: discovery, authorization request, token exchange, ID-token verification |
| `app/auth/session.py` | The signed, HttpOnly application-session cookie |
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
| `/api/chat`, `/api/query`, `/api/v1/query`, `/api/catalog`, `/api/labels`, `/api/search`, `/api/proposals*`, `/api/conversations*`, `/api/alerts*`, `/api/alert-proposals*`, `/api/v1/alerts/confirm`, `/api/adk/*`, `/api/v1/admin/*` | Requires an authenticated identity |

An unauthenticated API call is answered with `401`, never with data. No
identity header sent by the browser is ever believed.

The identity is published on `request.state.principal` (and returned by
`/api/auth/whoami`) as the stable `(issuer, subject)` pair, so a later
authorization layer can build on it without changing authentication.
Authentik's group and entitlement claims are **not** read by the
application: Authentik verifies identity, it does not hand the application
its privileges.

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
  *this application's* session. It is **not** a global single-sign-on
  logout: the user stays signed in to Authentik and to every other
  application that uses it, unless the Authentik provider's invalidation
  flow is configured to end that session too.

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

### Authorization (RBAC) is out of scope

A successful login establishes *identity only*. It does not enforce
least-privilege authorization: every authenticated user currently has the
same access to every endpoint. Roles, permissions and the authorization UI
are a separate piece of work.
