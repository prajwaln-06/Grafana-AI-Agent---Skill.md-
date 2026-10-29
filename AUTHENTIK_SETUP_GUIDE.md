# Authentik Setup Guide — OpenID Connect authentication for the Grafana AI Agent

This guide describes how to authenticate the Grafana AI Agent against an
Authentik instance with **OpenID Connect (Authorization Code flow)**.

It replaces the former *nginx forward-auth* setup. **There is no reverse
proxy in the request path any more**: the vite development server serves the
UI *and* proxies `/api` to the FastAPI backend, so the browser only ever
talks to one origin — which is exactly what the OIDC redirect URI needs.

---

## 0. Architecture at a glance

```
                    ┌───────────────────────────────────────────┐
                    │  Authentik  (its OWN compose project)    │
                    │  http://localhost:9000                    │
                    │  · login pages / enrollment / password    │
                    │  · OIDC discovery, JWKS, token endpoint   │
                    └───────────────▲───────────────────────────┘
                                    │  1. Authorization Code flow
                                    │     (browser is redirected)
                    ┌───────────────┴───────────────────────────┐
                    │  Browser                                  │
                    │  http://localhost:5173  (the app origin)  │
                    └───────────────┬───────────────────────────┘
                                    │  2. GET /api/auth/callback?code=…
                                    ▼
                    ┌───────────────────────────────────────────┐
                    │  vite dev server :5173  ──/api──▶ FastAPI │
                    │  (serves the UI, proxies /api)     :8008   │
                    └───────────────────────────────────────────┘
```

| Port | What | Who reaches it |
|---|---|---|
| **5173** | The application: vite dev server + `/api` proxy | the browser — **the only normal application URL** |
| **8008** | FastAPI backend | the vite dev server (and `curl`, for verification) |
| **9000** | Authentik: server, admin interface, login pages | the browser |

### The endpoints this application exposes

| Endpoint | Purpose |
|---|---|
| `GET /api/auth/config` | Public configuration for the login screen (login path, enrollment URL, password-change URL). No secret is ever returned here. |
| `GET /api/auth/login` | Starts the OIDC Authorization Code flow; redirects the browser to Authentik. |
| `GET /api/auth/callback` | The **redirect URI**. Validates the response and establishes the application session. |
| `GET /api/auth/whoami` | The verified identity of the caller (email, stable subject id). Requires a session. |
| `POST /api/auth/logout` | Ends the **application** session. Requires nothing else; it only clears this application's cookie. |

### What the backend does with Authentik's answer

* The authorization request carries a fresh `state`, a fresh `nonce` and a
  PKCE challenge (S256).
* The callback rejects a `state` that does not match the login attempt it
  belongs to.
* The authorization code is exchanged for tokens at Authentik's token
  endpoint; the client secret never leaves the backend.
* The ID token's **signature** is verified against Authentik's JWKS, and its
  `iss`, `aud`, `exp` and `nonce` claims are checked. An ID token is never
  decoded without being verified.
* Only then is a session established: a signed, **HttpOnly** cookie holding
  the stable identity (`sub`, `iss`) and the profile fields the UI shows.
  No access token, refresh token or ID token is stored in it, and none of
  them is ever sent to the browser. Nothing is written to `localStorage` or
  `sessionStorage`.

### Out of scope: authorization (RBAC)

A successful login establishes **identity only**. It does **not** enforce
least-privilege authorization: every authenticated user currently reaches
every protected endpoint. Roles, permissions and the authorization UI are a
separate piece of work and are explicitly **not** part of this guide.

---

## 1. Prerequisites

### Linux

* Docker Engine with the Compose plugin (`docker compose version`).
* Python 3.10+ and Node.js 18+ with npm (for the backend and the frontend).
* Ports **9000** (Authentik), **5173** (application) and **8008** (backend)
  free on the host.

### Windows (Docker Desktop with WSL2)

* Docker Desktop with the WSL2 backend enabled.
* Run the commands inside a WSL2 shell (Ubuntu) — the repository lives on
  the Linux filesystem for acceptable I/O performance.
* Port forwarding: Docker Desktop publishes the container ports on
  `localhost`, so `http://localhost:9000` and `http://localhost:5173` work
  from the Windows browser as well.

### macOS

* Docker Desktop (or Colima / Rancher Desktop).
* Ports **9000**, **5173** and **8008** free.

---

## 2. Start Authentik in its own Compose project

Authentik is **not** part of this repository's compose project. It runs as a
separate stack, so the two can be started, stopped and upgraded
independently.

1. Get the official Authentik compose file (this repository does not vendor
   it, and this guide does not modify a running instance):

   ```bash
   git clone https://github.com/goauthentik/authentik.git
   cd authentik
   ```

2. Create the environment file and the local configuration directory:

   ```bash
   mkdir -p ./authentik_media
   export AUTHENTIK_SECRET_KEY="$(openssl rand -base64 42)"
   echo "AUTHENTIK_SECRET_KEY=${AUTHENTIK_SECRET_KEY}" > .env
   ```

3. Start Authentik:

   ```bash
   docker compose up -d
   ```

   The web interface (and the admin interface) is then on
   `http://localhost:9000`. The first start creates the initial admin user
   (`akadmin` / whatever password the setup asks for) — the exact steps
   depend on the Authentik version you run.

4. In the same Authentik directory, add the ports for your host if the
   compose file does not publish them already:

   ```yaml
   # authentik/docker-compose.yml (ports section of the "server" service)
   ports:
     - "9000:9000"
     - "9443:9443"
   ```

   > The application talks to Authentik over plain HTTP on `http://localhost:9000`,
   > which is why the discovery URL and the redirect URI below use `http://`.
   > Authentik must be reachable at exactly the URL you configure — the
   > issuer in the ID token has to match the discovery document's `issuer`.

> **Do not** merge the Authentik stack into this repository's
> `docker-compose.yml`, and do not modify a running Authentik instance with
> `docker exec` / SQL. Every configuration step below is performed in the
> Authentik **admin interface** so that it is reproducible and reviewable.

---

## 3. Create the OIDC provider and the application

Everything in this section happens in the Authentik admin interface at
`http://localhost:9000/admin/`.

### 3.1 The provider (OAuth2 / OpenID Connect)

1. **Admin → Providers → Create**
2. **Type:** `OAuth2/OpenID Connect`
3. **Name:** `grafana-ai-agent`
4. **Authorization flow:** the provider flow that issues an *authorization
   code*. The stock Authentik provider flow
   (`default-provider-authorization-implicit-consent`) contains the
   "Authorize code" stage; if you have created your own provider flows,
   pick one that does.
5. **Invalidation flow:** the flow that ends an Authentik session, e.g.
   `default-provider-invalidation-flow` (or the sign-out flow of your
   instance). This is what makes a *global* sign-out possible; the
   application's own "Sign out" button is local only (see §10.4).
6. **Client type:** `Confidential` — a confidential client has a client
   secret, which is what the backend uses at the token endpoint.
7. **Redirect URI:** exactly

   ```
   http://localhost:5173/api/auth/callback
   ```

   The redirect URI has to match **character for character** — scheme, host,
   port and path. `http://localhost:5173` (without the path) or
   `http://localhost:5174/api/auth/callback` are *not* the same thing, and
   Authentik will refuse the request.
8. Leave the **signing key** on the default (Authentik then publishes the
   matching public key in its JWKS).
9. **Save.**

### 3.2 The application

1. **Admin → Applications → Create**
2. **Type:** `Regular Web Application`
3. **Name:** `Grafana AI Agent`
4. **Slug:** `grafana-ai-agent` — the slug is part of the discovery URL.
5. **Provider:** the provider from §3.1.
6. **User settings flow:** `default-password-change` (see §6.2). Leaving it
   empty means the *Change password* action has no flow to open.
7. **Save.**

### 3.3 Verify the discovery document

Open, in a browser:

```
http://localhost:9000/application/o/grafana-ai-agent/.well-known/openid-configuration
```

You should get a JSON document. Check that:

* `issuer` is `http://localhost:9000/application/o/grafana-ai-agent/`,
* `authorization_endpoint`, `token_endpoint` and `jwks_uri` are present,
* `response_types_supported` contains `"code"`,
* `code_challenge_methods_supported` contains `"S256"` (PKCE).

If the URL returns `404`, the application slug is different from
`grafana-ai-agent` — use the slug that is actually shown in the application
list. **Use the URL that answers** as `AUTHENTIK_OIDC_DISCOVERY_URL`; do not
assume it.

---

## 4. Client ID and client secret

Both live on the **provider** page:

* **Admin → Providers → `grafana-ai-agent`** → the fields **Client ID** and
  **Client Secret**.

They are generated by Authentik. Copy them into this repository's `.env`:

```bash
AUTHENTIK_OIDC_CLIENT_ID=<the client id from the provider page>
AUTHENTIK_OIDC_CLIENT_SECRET=<the client secret from the provider page>
```

The client secret is a credential: it stays on the server, it is never sent
to the browser, and it is never returned by `/api/auth/config`. Keep it out
of version control — `.env` is git-ignored.

---

## 5. The application's configuration

In this repository:

```bash
cp .env.example .env    # first time only
```

Then set:

```dotenv
# --- Authentication -------------------------------------------------------
AUTHENTIK_ENABLED=true
AUTHENTIK_BASE_URL=http://localhost:9000
AUTHENTIK_APP_SLUG=grafana-ai-agent

# The discovery document of the provider that is attached to the application.
# Verify it in a browser first (see §3.3).
AUTHENTIK_OIDC_DISCOVERY_URL=http://localhost:9000/application/o/grafana-ai-agent/.well-known/openid-configuration

AUTHENTIK_OIDC_CLIENT_ID=<from the provider page>
AUTHENTIK_OIDC_CLIENT_SECRET=<from the provider page>

# Must be identical to the redirect URI registered in Authentik (§3.1).
AUTHENTIK_OIDC_REDIRECT_URI=http://localhost:5173/api/auth/callback

AUTHENTIK_OIDC_SCOPE=openid profile email
AUTHENTIK_OIDC_PKCE=true

# Secret that signs the application-session cookie. Generate one:
#   openssl rand -hex 32
AUTHENTIK_SESSION_SECRET=<a long random value>
AUTHENTIK_SESSION_MAX_AGE_SECONDS=3600
AUTHENTIK_SESSION_COOKIE_NAME=grafana_ai_session
AUTHENTIK_SESSION_COOKIE_SECURE=false
AUTHENTIK_SESSION_COOKIE_SAMESITE=lax
```

Notes:

* `AUTHENTIK_SESSION_SECRET` signs the session cookie. Changing it
  invalidates every existing session.
* `AUTHENTIK_SESSION_COOKIE_SECURE=false` is correct for the local
  `http://localhost` setup. Set it to `true` as soon as the application is
  served over HTTPS.
* `AUTHENTIK_SESSION_COOKIE_SAMESITE=lax` is required: the browser has to
  send the cookie on the top-level redirect back from Authentik.
* With `AUTHENTIK_ENABLED=true` and a missing client id, client secret,
  discovery URL, redirect URI or session secret, the backend **refuses to
  start** and says which value is missing. That is deliberate: it fails
  closed rather than serving unauthenticated traffic.
* While you are still setting Authentik up, `AUTHENTIK_ENABLED=false` keeps
  the local workflow usable. It is a development convenience, **not** a
  security control.

---

## 6. Enrollment ("Create account") and password change

Both flows live in Authentik. This application never collects a password:
there is no registration API and no password API in the backend.

### 6.1 Enrollment flow (self-service sign-up)

1. **Admin → Flows and Stages → Flows → Import**
2. Import the blueprint `example/flows-enrollment-2-stage.yaml` (Authentik
   ships it in its `blueprints` directory). It creates an enrollment flow
   with the slug **`default-enrollment-flow`**.
   *If you prefer to build it by hand: create a flow with designation
   `enrollment`, slug `default-enrollment-flow`, policy engine mode
   `global`, and add the stages "User enrollment" (asks for username and
   email) and "User write" (creates the account).*
3. Open `http://localhost:9000/if/flow/default-enrollment-flow/` and check
   that you get a form asking for an email address and a password. If you
   do not, the flow is not wired as an enrollment flow (check its
   designation and its stages).
4. The application builds the link from `AUTHENTIK_BASE_URL` +
   `AUTHENTIK_ENROLLMENT_FLOW`, so with the defaults the link is
   `http://localhost:9000/if/flow/default-enrollment-flow/`. Override it
   with `AUTHENTIK_SIGNUP_URL` if your flow needs extra query parameters.

When no enrollment flow is configured, the login screen **disables** the
"Create account" action and says so, instead of linking to a page that does
not exist.

### 6.2 Password-change flow

1. Check whether the flow `default-password-change` exists
   (**Admin → Flows and Stages → Flows**). If it does not, create a flow
   with designation `user settings` and slug `default-password-change`, and
   add the stage **Password change** (it asks for the current password and
   the new one).
2. Set it as the **User settings flow** of the application:
   **Admin → Applications → `grafana-ai-agent` → User settings flow →
   `default-password-change`**. Without this, an ordinary user cannot
   reach the flow.
3. Verify: while signed in to Authentik, open
   `http://localhost:9000/if/flow/default-password-change/` — you should
   see the "Change password" form.
4. The application opens the flow from
   `AUTHENTIK_BASE_URL` + `AUTHENTIK_PASSWORD_CHANGE_FLOW`
   (`http://localhost:9000/if/flow/default-password-change/`), or from
   `AUTHENTIK_PASSWORD_CHANGE_URL` if you set one.

### 6.3 Branding (optional)

Authentik's own login and enrollment pages are branded through
**Admin → Branding**. Set the application name, logo and background there
so that the Authentik pages match the application. The application's own
login screen is branded separately in `ui/src/components/LoginScreen.tsx`.

---

## 7. Run the frontend and the backend

### 7.1 Everything at once

```bash
cd /path/to/grafana-ai-agent
./start.sh
```

`./start.sh` starts the FastAPI backend on `:8008` and the vite dev server
on `:5173`.

### 7.2 Separate terminals

Backend (terminal 1):

```bash
cd /path/to/grafana-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PORT=8008 API_PORT=8008 python run_server.py
```

Frontend (terminal 2):

```bash
cd /path/to/grafana-ai-agent/ui
npm install
npm run dev:web        # vite only, on :5173
# or: npm run dev      # vite (:5173) plus the legacy express API (:5050)
```

### 7.3 The infrastructure (Prometheus, Grafana, Loki, OpenSearch)

Unchanged and unrelated to authentication:

```bash
docker compose up -d --build
```

---

## 8. Use the application

Open **`http://localhost:5173`**.

That is the only URL you need: the vite dev server serves the UI and
proxies `/api` to the backend, so the OIDC callback
(`http://localhost:5173/api/auth/callback`) reaches the backend through the
same origin. Do **not** open the backend directly on `:8008` for the login
flow — the redirect URI would not match.

---

## 9. Manual verification

Work through this list after the configuration is complete.

| # | Step | Expected result |
|---|---|---|
| 1 | Open `http://localhost:5173` | The branded login screen with the heading **Login**, a **Login with Authentik** button and a **Create account** link |
| 2 | Click **Login with Authentik** | The browser is redirected to `http://localhost:9000/...` and Authentik's own login form appears |
| 3 | Sign in with an Authentik user | The browser returns to `http://localhost:5173/` and the dashboard is shown |
| 4 | `curl -i http://localhost:5173/api/auth/whoami` **without** a session | `401` with a JSON `detail` |
| 5 | In the browser: `fetch('/api/auth/whoami').then(r => r.json()).then(console.log)` | The identity: `subject`, `email`, `username`, `issuer`, `authenticated: true` |
| 6 | Click the profile icon (top right) | The menu shows the **real** email address from `/api/auth/whoami` |
| 7 | Click **Change password** | Authentik's password-change flow opens (the password is entered on Authentik) |
| 8 | Click **Sign out** | The application session ends and the login screen is shown again |
| 9 | After signing out, `curl -i http://localhost:5173/api/auth/whoami` | `401` |
| 10 | Sign out, then click **Create account** | Authentik's enrollment form asks for email and password; the account is created in Authentik |
| 11 | Sign in with the new account | The dashboard opens; the profile menu shows the new address |

Backend-only check (no browser):

```bash
# The public login configuration -- no secret is exposed
curl -s http://localhost:5173/api/auth/config | python3 -m json.tool

# The protected endpoint without a session
curl -s -i http://localhost:5173/api/auth/whoami | head -1
```

---

## 10. Troubleshooting

### 10.1 `redirect_uri` mismatch

*Symptom:* Authentik shows an error such as `invalid_request: The redirect
uri does not match any of the configured ones`, or the callback returns
`auth_error=provider_error` in the URL.

*Cause:* the redirect URI registered in Authentik and the one the
application sends are not identical.

*Fix:* compare them character by character:

| In Authentik (provider) | In `.env` |
|---|---|
| `http://localhost:5173/api/auth/callback` | `AUTHENTIK_OIDC_REDIRECT_URI=http://localhost:5173/api/auth/callback` |

Watch for a trailing slash, a different port, `https` vs `http`, and
`localhost` vs `127.0.0.1` (they are different origins as far as the
provider is concerned).

### 10.2 Invalid client credentials

*Symptom:* the callback fails with `auth_error=token_exchange_failed`, and
the backend log says the authorization code was rejected.

*Causes and fixes:*

* the client secret in `.env` is wrong or was rotated in Authentik — copy it
  again from **Admin → Providers → `grafana-ai-agent`**;
* the provider is a **public** client — it must be **confidential**, so that
  a secret exists;
* the client id belongs to a different provider than the one whose
  discovery URL you configured — the client id and the discovery URL must
  describe the same provider.

### 10.3 State or nonce errors

*Symptom:* the callback returns `auth_error=state_mismatch` or
`auth_error=id_token_invalid`; in the browser the login screen shows the
error.

*Cause:* the callback did not belong to the login attempt that is being
completed — usually a login that was started in another browser or tab, a
login that was restarted, or a cookie that did not survive the round trip.

*Fixes:*

* start the login again in the same browser, and do not open the
  authorization URL in a second browser;
* make sure the browser accepts cookies for `http://localhost:5173` — the
  login transaction is kept in a signed cookie;
* if you changed `AUTHENTIK_SESSION_SECRET` while a login was in flight, the
  transaction cookie can no longer be verified — start the login again.

### 10.4 `401` from the API

*Symptom:* the UI shows the login screen although you just signed in, or
`curl` answers `401`.

*Causes and fixes:*

* **no session cookie** — the session is `HttpOnly` and scoped to
  `http://localhost:5173`; the API calls must therefore go to the *same*
  origin (`http://localhost:5173/api/...`), not to
  `http://localhost:8008/api/...`;
* **the session expired** — the default lifetime is 3600 seconds
  (`AUTHENTIK_SESSION_MAX_AGE_SECONDS`);
* **the session secret changed** — every existing session became invalid;
* **the backend runs in development mode** — with `AUTHENTIK_ENABLED=false`
  the API is open and `/api/auth/whoami` reports `authenticated: false`;
* **the backend is not configured** — a misconfigured deployment answers
  `503`, not `401`, and refuses to start at all.

### 10.5 `403` responses

The application itself never returns `403`: it does not implement
authorization. A `403` therefore comes from Authentik — typically a
**policy** attached to the provider or the application that denies the user
(an "allowed user" / "allowed group" policy). Check
**Admin → Applications → `grafana-ai-agent` → Policies** and the policies of
the provider, and attach the user to the group the policy allows.

### 10.6 The backend is unavailable

*Symptom:* the login screen renders but every API call fails, or the UI
shows "The UI API is unreachable".

*Fixes:*

* is the backend running? `curl -s http://localhost:5173/healthz` (through
  the app origin) or `curl -s http://localhost:8008/healthz`;
* is it listening on `:8008`? `lsof -i :8008`;
* did the backend start at all? `tail -40 server.log` — with
  `AUTHENTIK_ENABLED=true` and a missing OIDC value it refuses to start and
  names the missing variable;
* does the vite proxy point at the right backend? the default is
  `http://127.0.0.1:8008`; override it with `AGENT_DEV_API_TARGET`.

### 10.7 The Authentik flows are missing

*Symptom:* the "Create account" link is disabled, or the *Change password*
button does nothing, or Authentik shows "flow not found".

*Fix:* the corresponding flow does not exist or is not reachable — see §6.1
(enrollment) and §6.2 (password change). Both are Authentik-side
configuration; the application only links to them.

### 10.8 Authentik is not reachable from the backend

The backend fetches the discovery document and the JWKS from Authentik at
login time. If Authentik is down, `/api/auth/login` answers `503` and the
login screen shows the error. Check `http://localhost:9000` and the backend
log.

---

## 11. What this guide does not cover

* **RBAC / authorization.** A successful login proves *who* the user is. It
  grants no privileges. Roles, permissions, role management and the
  authorization UI are a separate piece of work; until they exist, every
  authenticated user can reach every protected endpoint.
* **A global single-sign-on logout.** The application's "Sign out" clears
  this application's session only. The user stays signed in to Authentik
  itself (and to other applications). Ending the Authentik session as well
  is configured on the provider side (its invalidation flow / a
  user-settings flow), not by this application.
* **Production hardening.** The guide describes a local development setup.
  For anything reachable from outside your machine: use HTTPS, set
  `AUTHENTIK_SESSION_COOKIE_SECURE=true`, keep the client secret and the
  session secret out of version control, and narrow
  `AUTHENTIK_OIDC_SCOPE` to what the application really needs.
