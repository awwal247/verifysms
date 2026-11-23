# VerifySMS — Django application

This package is the VerifySMS frontend built as a Django application:

- Django sessions for registration, login, logout, active/banned users, and admin authorization
- SQLite by default, with Django ORM models for users, orders, wallet transactions, top-ups, settings, coupons, and coupon usage
- Atomic wallet debit/credit operations with a ledger entry for every purchase, refund, top-up, and adjustment
- Two-provider routing: SMS-BUS (server 1) with automatic 5SIM (server 2) fallback — the catalogue, cheapest-price quote and every purchase fail over transparently, and customers never pick a server
- Paystack initialization, verification, webhook signature validation, idempotent top-up crediting, and fee calculation
- Admin actions for users, balance adjustments, coupons, provider settings, payment settings, and site settings

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

`seed_demo` has no built-in passwords. Create the first admin with:

```bash
python manage.py seed_demo --email you@yourdomain.com          # generates a random password, printed once
python manage.py seed_demo --email you@yourdomain.com --password '<strong password>'
# or: SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD environment variables
python manage.py seed_demo --email you@yourdomain.com --with-test-user   # local only: Paystack test-card customer
```

Re-running it is safe: existing passwords and edited settings are left alone
(`--reset-password` replaces an admin's password; `--promote` upgrades an
existing customer account). For local development set `DJANGO_DEBUG=1` and
`DJANGO_SECRET_KEY` is optional; in production `DJANGO_SECRET_KEY` is required.

The service browser reads 5SIM's public catalogue when `FIVESIM_API_KEY` is
empty, so it can be explored immediately. Buying needs a key on at least one
server; when neither key is set, purchases run in demo mode. Add keys in the
environment or in Admin → Providers. Routing is automatic: the selected server
is tried first, the other takes over on any provider error or empty stock. Paystack requires
both `PAYSTACK_PUBLIC_KEY` and `PAYSTACK_SECRET_KEY`.

## Configuration

Copy `.env.example` to your environment and set:

- `DJANGO_SECRET_KEY`
- `ALLOWED_HOSTS`
- `SMSBUS_API_KEY` (server 1 — primary)
- `FIVESIM_API_KEY` (server 2 — automatic fallback)
- `PAYSTACK_PUBLIC_KEY`
- `PAYSTACK_SECRET_KEY`
- `SITE_URL`
- `DATABASE_URL` (optional — see below)

### Database: Supabase Postgres with a local SQLite fallback

The app uses `DATABASE_URL` to pick its database:

- **Unset / empty** → falls back to the bundled SQLite file (`db.sqlite3`).
  Nothing to configure; this is what a fresh `git clone` gets for local dev.
- **Set to a Supabase Postgres connection string** → the app uses that
  instead. Get it from Supabase: Project Settings → Database → Connection
  string → URI. Use the **pooler** URL (port 6543, `...pooler.supabase.com`)
  for production/serverless deployments; the direct connection (port 5432)
  works fine for local development against a shared Supabase project.

```bash
DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres
```

After setting it, run migrations against Supabase the same way as SQLite:

```bash
python manage.py migrate
python manage.py seed_demo   # optional demo data
```

SSL is required by default when `DATABASE_URL` is set (`DB_SSL_REQUIRE=1`),
which matches Supabase's requirement. Leave it at `1` unless you have a
specific reason to disable it.

## Tests

Two suites, split by what they need installed (full guide: [`docs/TESTING.md`](docs/TESTING.md)):

```bash
python scripts/run_tests.py --standalone   # NO dependencies: bare Python 3.10+, standard library only
python scripts/run_tests.py --full         # WITH dependencies (after pip install -r requirements.txt)
python scripts/run_tests.py                # standalone, plus full when dependencies are present
python scripts/run_tests.py --live https://yourdomain.com   # optional read-only header/TLS/DNS probe
```

Every run writes a plain-text report to `TEST_REPORT.txt`. Equivalent direct commands:

```bash
python -I -m unittest discover -s tests_standalone -t . -v
DJANGO_DEBUG=1 python manage.py test core -v 2
```

| Suite | Location | Needs |
|---|---|---|
| Standalone | `tests_standalone/` | Python only: static checks of source, templates, JS, settings, migrations, routes, env docs |
| Full | `core/tests/` | Django, dj-database-url, psycopg: HTTP, wallet, routing, Paystack, settings per environment, migrations |
| Live | `scripts/security_check.py` | Network access to your deployed site |

No test calls a real provider or Paystack.

## Project layout

```
core/                  app: models, views, services (providers, wallet, Paystack), middleware, tests/
verify_sms/            Django settings, urls, wsgi
templates/ assets/     pages and static files (served under /assets/)
tests_standalone/      dependency-free tests
scripts/run_tests.py   test runner + TEST_REPORT.txt writer
scripts/security_check.py   live security probe (stdlib only)
docs/                  SECURITY_HARDENING.md, TESTING.md
```

## Main routes

`/`, `/login.html`, `/register.html`, `/user/dashboard.html`,
`/user/services.html`, `/user/cart.html`, `/user/topup.html`,
`/user/history.html`, `/user/settings.html`, `/user/sms.html`,
`/admin/dashboard.html`, `/admin/users.html`, `/admin/transactions.html`,
`/admin/providers.html`, `/admin/payment.html`, `/admin/coupons.html`,
`/admin/broadcast.html`, and `/admin/settings.html`. Unknown pages return the 404 template.

Utility endpoints: `/healthz/`, `/robots.txt`, `/.well-known/security.txt`, `/security.txt`,
and the JSON/webhook APIs under `/api/` (`orders`, `provider`, `coupon`, `support`, `paystack/*`).
`/deploy-setup/` is inert unless `DEPLOY_SETUP_TOKEN` is set (see `.env.example`).

## Environment variables needed to deploy

Two different kinds of configuration exist here, and they're set in two
different places on purpose:

- **Infrastructure config (env vars only).** These have to exist before the
  app can even connect to a database or start serving requests, so they
  can't live in the database itself — set them as real environment
  variables on your host (Vercel/Render/wherever).
- **Business config (admin page, not env vars).** Paystack keys, fees,
  and top-up limits are stored in the `Setting` database table and are
  editable from `/admin/settings.html` and `/admin/payment.html` without a
  redeploy. `PAYSTACK_PUBLIC_KEY`/`PAYSTACK_SECRET_KEY` env vars still exist
  as a fallback (used only if nothing has been saved in admin yet), but
  once you save real keys in the admin UI those take priority.

Required env vars for a production deploy:

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django's cryptographic signing key. Generate a long random value; never reuse the dev default. |
| `DJANGO_DEBUG` | Set to `0` in production. Leaving debug on publicly exposes stack traces. |
| `ALLOWED_HOSTS` | Comma-separated list of domains allowed to serve the app, e.g. `verifysms.com,www.verifysms.com`. |
| `SITE_URL` | Your public HTTPS URL, e.g. `https://verifysms.com`. Used to build the Paystack callback URL — must be HTTPS for live Paystack keys to work. |
| `DATABASE_URL` | Your Supabase Postgres connection string. Leave unset only for local SQLite dev. |
| `DB_SSL_REQUIRE` | Leave at `1` (default) for Supabase. |
| `DB_CONN_MAX_AGE` | Optional; connection reuse in seconds (default `60`). |
| `SMSBUS_API_KEY` | SMS-BUS token (server 1, primary) for buying real phone numbers and reading its catalogue. Can also be set later via `/admin/providers.html`. |
| `FIVESIM_API_KEY` | 5SIM key (server 2, automatic fallback). Catalogue and prices work without it; buying needs it. Used whenever the selected server errors, times out or has no stock. Can also be set later via `/admin/providers.html`. |

Optional (fallback only — prefer setting these in `/admin/payment.html` once deployed):

| Variable | Purpose |
|---|---|
| `PAYSTACK_PUBLIC_KEY` | Only used if no key has been saved in admin yet. |
| `PAYSTACK_SECRET_KEY` | Only used if no key has been saved in admin yet. |

Optional tuning (all documented in `.env.example`): `SECURITY_CONTACT` (set a monitored `mailto:` for
`security.txt`), `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD`,
`CSP_REPORT_ONLY`, `CSP_REPORT_URI`, `CSRF_TRUSTED_ORIGINS`, `SESSION_COOKIE_AGE`, `SECURE_SSL_REDIRECT`,
`TRUST_PROXY_SSL_HEADER`, `REDIS_URL`. A test fails if the code reads an environment variable that is not
documented in `.env.example` or this file.

## Known open items

- CSP still allows `'unsafe-inline'` until inline handlers move to external JS (see `docs/SECURITY_HARDENING.md`).
