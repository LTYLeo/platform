# TAI Developer Platform — backend

FastAPI + SQLite authentication service for the TAI Developer Platform. It serves
the static site **and** the JSON API from one process, so the session cookie is
first-party and there is no CORS to configure in production.

## Quick start

```bash
cd "Developer Platform"          # the folder containing index.html
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r server/requirements.txt
python3 -m uvicorn server.main:app --reload --port 8000
```

Then open <http://127.0.0.1:8000/>.

The SQLite file is created automatically at `server/data/app.db` on first boot.

## Dependencies

Only three, and none of them compile:

| Package | Why |
|---|---|
| `fastapi` | routing, request validation |
| `uvicorn` | ASGI server |
| `pydantic` | request/response schemas |

Password hashing uses `hashlib.scrypt` and storage uses `sqlite3` — both from the
standard library. There is no `bcrypt`/`passlib` wheel to build, which keeps
deployment to "copy the folder and install three pure-Python packages".

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | – | liveness probe |
| `POST` | `/api/auth/register` | – | create an account, starts a session |
| `POST` | `/api/auth/login` | – | sign in |
| `POST` | `/api/auth/logout` | cookie | destroy the session **server-side** |
| `GET` | `/api/auth/me` | cookie | current user, or `401` |
| `GET` | `/api/keys` | cookie | list active API keys |
| `POST` | `/api/keys` | cookie | mint a key — the plaintext is returned **once** |
| `DELETE` | `/api/keys/{id}` | cookie | revoke a key |
| `GET` | `/api/usage` | cookie | real usage figures for the dashboard |
| `GET` | `/api/v1/models` | `Bearer sk-tai-…` | validate an API key and list live models |

Errors always use one shape, so the bilingual front-end has a single thing to
translate:

```json
{ "code": "invalid_credentials", "message": "Incorrect email or password" }
```

Interactive docs: <http://127.0.0.1:8000/api/docs>

## Security model

| Concern | How it is handled |
|---|---|
| Password storage | scrypt, `n=2^14 r=8 p=1`, 16-byte random salt, stored as `scrypt$n$r$p$salt$hash`. `needs_rehash()` upgrades old parameters on next login. |
| Session theft from DB | The cookie holds a random 32-byte token; the DB stores only its SHA-256, so a database leak cannot be replayed as a session. |
| XSS reading the cookie | Cookie is `HttpOnly`. |
| CSRF | `SameSite=Lax` **and** an explicit `Origin`/`Host` match on every state-changing request. |
| Credential stuffing | 8 failed attempts per (email, IP) in a 15-minute window → `429`. |
| User enumeration | Unknown email and wrong password return the identical `invalid_credentials` response, and an unknown email still pays the cost of a scrypt hash. |
| Timing attacks | `hmac.compare_digest` for hash comparison. |
| Leaking the database | `/server`, `/data`, `/__pycache__`, `/.git`, `/.env` and any dotted path are blocked **before** the static mount. |

### Before you deploy

1. **Set `TAI_COOKIE_SECURE=true`** and serve over HTTPS. The cookie is
   `Secure`-gated by that variable, and off by default only so local
   `http://127.0.0.1` works.
2. Put a TLS-terminating reverse proxy (Caddy, nginx, Cloudflare) in front. If it
   sets `X-Forwarded-For`, the rate limiter already honours it.
3. Back up `server/data/` — that single file is your entire user database.
4. Consider adding `--workers N` behind the proxy once traffic justifies it.
   SQLite in WAL mode handles this workload comfortably; move to Postgres only if
   you start writing heavily from many workers.

## Deploying — GitHub Pages (front-end) + your own machine (API)

GitHub Pages serves **static files only**; it cannot run this Python service. The
intended split is:

```
  GitHub Pages  (static)                 your machine (home)
  https://dev.yourdomain.com             https://api.yourdomain.com
            │                                       │
            └──── fetch(credentials:'include') ─────▶ uvicorn  ──▶ server/data/app.db
                                  ▲
                          Cloudflare Tunnel / frp / ngrok
```

### The one thing that will break you: cookies

The session cookie defaults to `SameSite=Lax`. Browsers only attach a Lax cookie
to **same-site** requests. `ltyleo.github.io` → `api.something-else.com` is
*cross-site*, so login appears to succeed and is then silently forgotten on the
next request.

**Option A — recommended: one registrable domain.**

* GitHub Pages with a custom domain → `dev.yourdomain.com`
  (repo → Settings → Pages → Custom domain)
* Tunnel exposing the API on → `api.yourdomain.com`
* Both share `yourdomain.com`, so they are same-site and `SameSite=Lax` just
  works. Cloudflare Tunnel gives you a stable subdomain of your own domain for
  free and handles TLS.

**Option B — fallback: third-party cookies.**

```bash
TAI_COOKIE_SAMESITE=none TAI_COOKIE_SECURE=true
```

Works in Chrome and Firefox today, but **Safari and every browser on iOS block
third-party cookies outright**, and Chrome is phasing them out. Fine for a demo,
not for real users. The service refuses to start with `SameSite=None` unless
`TAI_COOKIE_SECURE=true`, because browsers reject that combination.

### Running the API behind the tunnel

```bash
export TAI_SERVE_SITE=false                     # API only; pages come from Pages
export TAI_COOKIE_SECURE=true                   # the tunnel provides HTTPS
export TAI_COOKIE_SAMESITE=lax                  # 'none' only for option B
export TAI_ALLOWED_ORIGINS=https://dev.yourdomain.com
export TAI_ENABLE_DOCS=false                    # optional: hide /api/docs

python3 -m uvicorn server.main:app \
  --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips='*' \
  --workers 2
```

* `--proxy-headers --forwarded-allow-ips='*'` matters: the tunnel terminates TLS
  and adds `X-Forwarded-For`. Without it every request looks like it comes from
  the tunnel's own local address, and the login rate limiter would then treat all
  visitors as a single attacker.
* **Never use `--reload` in production.** Bind to `127.0.0.1` and let the tunnel
  reach it locally rather than opening the port on your router.
* Point the tunnel at `http://127.0.0.1:8000`.

### Point the front-end at the API

Edit [`config.js`](../config.js) in the site root:

```js
window.TAI_CONFIG = { apiBase: 'https://api.yourdomain.com' };
```

Leave it `''` when one process serves both (local development).

### Publishing the site

`.github/workflows/pages.yml` assembles a `_site/` directory containing **only**
the front-end assets and publishes that. This is deliberate: the repository also
holds this backend and its database, and GitHub Pages serves everything in the
artifact. A guard in the workflow fails the build if `server/` sneaks in.

Enable it once: repo → Settings → Pages → Source → **GitHub Actions**.

### Home-hosted caveats

* Login is unavailable whenever your home power or connection drops. Run uvicorn
  under a supervisor (systemd, launchd, pm2, Docker `restart: unless-stopped`) so
  it comes back automatically.
* Back up `server/data/app.db` — that single file is your whole user database.
* Set `TAI_COOKIE_SECURE=true` only when the public URL is HTTPS (all common
  tunnels are).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TAI_COOKIE_SECURE` | `false` | send the cookie only over HTTPS — **set true in production** |
| `TAI_COOKIE_SAMESITE` | `lax` | `lax` \| `strict` \| `none`; `none` requires `TAI_COOKIE_SECURE=true` |
| `TAI_SESSION_DAYS` | `7` | session lifetime |
| `TAI_ALLOWED_ORIGINS` | *(empty)* | comma-separated front-end origins allowed to send credentialed requests. **Required when the site is on another domain.** |
| `TAI_SERVE_SITE` | `true` | set `false` to serve the API only |
| `TAI_ENABLE_DOCS` | `true` | serve `/api/docs` and the OpenAPI schema |

See `server/.env.example`.

## Front-end integration

* `config.js` — where the API lives (`apiBase`).
* `auth.js` — owns the session state, injects the sign-in/register modal on any
  page, and swaps the header's **Login** button for the user's name + **Logout**.
* `dashboard.html` — API keys from `/api/keys`, usage cards from `/api/usage`.
* All user-facing text goes through `TAIi18n.t()`, and error messages are keyed by
  the API's English `message`, so they translate automatically.

## Layout

```
config.js          front-end runtime config (API base URL)
api-endpoint.json  SDK discovery document (published on GitHub Pages)
_config.yml        Jekyll excludes — keeps server/ and sdk/ off the public site
sdk/               the tai-sdk Python package (published to PyPI)
server/
  main.py          FastAPI app: auth routes, middleware, static mount, wiring
  deps.py          shared dependencies: require_api_key, fail(), client_ip()
  api_v1.py        the Assistant API (assistants / threads / messages / chat)
  model_backend.py pluggable generation: stub | openai_compatible
  db.py            SQLite schema, login throttle, usage metering
  security.py      scrypt hashing, session tokens, API keys
  pricing.py       model catalogue + CNY cost calculation
  schemas.py       Pydantic request/response models
  requirements.txt
  .env.example
  data/app.db      created at runtime — back this up, never commit it
```

## The Assistant API

`server/api_v1.py` implements the contract in
[`sdk/API_CONTRACT.md`](../sdk/API_CONTRACT.md) — our own shape, not OpenAI's.
Authenticate with `Authorization: Bearer sk-tai-...`.

| Method | Path | Purpose |
|---|---|---|
| `POST`/`GET` | `/api/v1/assistants` | create / list assistants (model + instructions) |
| `GET`/`PATCH`/`DELETE` | `/api/v1/assistants/{id}` | fetch / update / delete |
| `POST`/`GET` | `/api/v1/threads` | create / list persistent conversations |
| `GET`/`PATCH`/`DELETE` | `/api/v1/threads/{id}` | fetch / update / delete |
| `POST`/`GET` | `/api/v1/threads/{id}/messages` | append + generate, or list history |
| `POST` | `/api/v1/chat` | stateless one-shot completion |
| `GET` | `/api/v1/models` | live models with pricing |
| `GET` | `/api/v1/usage` | this account's usage summary |

`stream: true` on `/chat` or the messages endpoint returns `text/event-stream`
with `message.start`, `message.delta`, `message.done` (and `error`) frames.

### Where the words come from

Nothing generates text until you attach a model. `TAI_MODEL_BACKEND` selects the
implementation:

* **`stub`** (default) — deterministic, offline, and deliberately labelled
  `[stub backend]` in every reply. It exists so the SDK, the streaming plumbing
  and the metering can be developed and tested before a model is served. It is
  never presented as real output.
* **`openai_compatible`** — forwards to a real server. Point
  `TAI_MODEL_BASE_URL` at vLLM (`http://host:8001/v1`), llama.cpp server,
  Ollama (`http://127.0.0.1:11434/v1`) or LM Studio, and the dashboard starts
  reporting genuine token counts and costs.

## Billing

Two consumption models, one payment layer.

| | Sigma | Developer Platform API |
|---|---|---|
| model | subscription (free / pro) | prepaid usage |
| unit | time | tokens |
| state | `subscriptions` | `credit_ledger` |
| gate | plan + daily limit | `db.can_generate()` |

Both start as a `payment_orders` row and end in a fulfilment that either adds
credit or extends a subscription.

### Why redeem codes exist

Collecting money in China as an individual is the awkward part: a personal QR
code may not be used for business collection, a merchant account normally needs
a business licence, and the channels that *do* work for individuals come and go.
So the money path is split:

```
money arrives however it arrives  ->  a code is issued  ->  the user redeems it
```

Adding or switching a payment channel therefore only changes *how a code is
issued*, never product code. `ManualProvider` needs no third party at all: an
admin confirms the transfer and the code or credit is granted, which also covers
bank transfers, cash, and "my friend paid for me".

### Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| `GET` | `/api/billing/entitlements` | user | plan, balance, free tokens, can-generate |
| `GET`/`POST` | `/api/billing/orders` | user | list / create a top-up or plan order |
| `POST` | `/api/billing/orders/{id}/intent` | user | how to pay (409 if the channel is unconfigured) |
| `POST` | `/api/billing/redeem` | user | redeem a code |
| `GET` | `/api/admin/providers` | admin | which channels are usable right now |
| `GET` | `/api/admin/orders` · `POST .../mark-paid` | admin | confirm a transfer arrived |
| `POST`/`GET` | `/api/admin/codes` | admin | mint / list codes |
| `GET` | `/api/admin/users` | admin | accounts with plan + balance |
| `POST` | `/api/admin/users/{id}/credit` · `/plan` | admin | grant credit / set plan |
| `GET` | `/api/admin/audit` | admin | who changed what |
| `GET` | `/api/internal/entitlements/{user_id}` | Sigma | shared-secret, so Sigma has no second copy of the plan |
| `POST` | `/api/payments/callback/{provider}` | channel | signed notification, idempotent |

### Staff CLI

Admin rights are a database flag with no self-service path — being first to
register must not hand someone the platform.

```bash
python3 -m server.admin_cli promote you@example.com
python3 -m server.admin_cli gen-codes api_credit 10 --amount 20
python3 -m server.admin_cli gen-codes sigma_pro 5 --months 1
python3 -m server.admin_cli orders --status pending
python3 -m server.admin_cli mark-paid ord_xxxx
python3 -m server.admin_cli users
```

### Safety properties

* **Replaying a payment callback cannot double-credit.** `provider_trade_no` is
  UNIQUE and `fulfil_order` short-circuits on an already-paid order. Channels
  retry aggressively, so this is load-bearing.
* **Redeeming twice cannot double-credit.** The redemption is a conditional
  `UPDATE ... WHERE redeemed_at IS NULL`, so a race has exactly one winner.
* **The free allowance is consumed before money.** A call is free while any of
  the 5,000 free tokens remain; the first call that starts after they run out is
  charged in full. Simple on purpose rather than prorating inside one call.
* **The internal API is closed when unconfigured**, not open.
* **Codes cannot be misread.** The alphabet omits `I L O 0 1`; input is
  normalised, so `tai - jcam - 467b - 236r` and `TAIJCAM467B236R` both work.

## Sigma integration

Sigma (the chat platform) keeps its own login but does **not** keep its own idea
of what a plan is worth. It asks the platform, so there is one billing authority
instead of two that drift apart.

The two systems are joined on **email**, because it is the only identifier they
already share: Sigma has its own account table and no knowledge of platform
integer ids.

| Endpoint | Used by | Purpose |
|---|---|---|
| `GET /api/internal/entitlements/by-email/{email}` | Sigma | plan, expiry, daily limit |
| `GET /api/internal/entitlements/{user_id}` | anything with the id | same, by id |
| `POST /api/internal/redeem` | Sigma | redeem `{email, code}` on the user's behalf |
| `GET /api/internal/health` | Sigma | which payment channels are configured |

All of them require `X-Internal-Token`, and the whole group returns **503
`internal_api_disabled` when `TAI_INTERNAL_TOKEN` is unset** — an unauthenticated
endpoint that discloses entitlements should not be one forgotten variable away.

### Sigma configuration

```bash
export TAI_PLATFORM_URL="http://<pi-lan-ip>:8000"
export TAI_INTERNAL_TOKEN="<same secret as the platform>"
```

### Behaviour that matters

* **The platform is authoritative when it answers.** Sigma's local `role` field
  is only consulted when the platform has no account for that email or cannot be
  reached. A plan bought on the platform therefore takes effect within one cache
  TTL (60 s), and a Pi outage degrades Sigma to its previous behaviour instead of
  locking every user out.
* **A failed lookup never creates an account.** Auto-provisioning would produce a
  platform account with no password that could never be logged into, which is
  worse than telling the user to register. Sigma surfaces
  `no_platform_account` as "register on the platform with the same email first".
* **Sigma never learns what a code is worth.** It forwards the string; the
  platform decides, validates and records. `redeem_codes` is the only place a
  code's value is defined.
* **Entitlement lookups are cached for 60 s** because `/generate` is hot; a
  lookup per generation would put the Pi on the critical path of every message.

## Not built yet

* **Sigma does not yet show the plan in its UI.** The backend routes
  (`/entitlements`, `/redeem`) exist and are verified; the front-end still needs a
  plan badge and a redeem box in `index.html`.
* **WeChat Pay Native** is deliberately a stub until signing credentials exist:
  ordering a payment without them produces QR codes that cannot be scanned.
  Afdian is written and needs `TAI_AFDIAN_TOKEN` + `TAI_AFDIAN_USER_ID`.
* **Invoices.** A Chinese VAT invoice needs tax registration, which needs a
  business licence.
* **Per-key rate limits.** Only login attempts are throttled today. Balance is
  enforced; rate is not.
* **Token counting is exact only for `openai_compatible`**, which takes the
  upstream's reported usage. The stub estimates with `ceil(len/4)` and marks the
  result `"estimated": true`.
