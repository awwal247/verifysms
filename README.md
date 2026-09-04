# VerifySMS — Django application

This package is the VerifySMS frontend built as a Django application:

- Django sessions for registration, login, logout, active/banned users, and admin authorization
- SQLite by default, with Django ORM models for users, orders, wallet transactions, top-ups, settings, coupons, and coupon usage
- Atomic wallet debit/credit operations with a ledger entry for every purchase, refund, top-up, and adjustment
- 5sim provider adapter for service/country/operator discovery, buying numbers, polling SMS, finishing, cancelling, and banning orders
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

The seeded development admin is `admin@example.com` with password
`ChangeMe123!`. A test user is also seeded: `testuser@example.com` with
password `TestUser123!` — log in as this user to test the Paystack top-up
flow with a Paystack test card (it starts with a ₦0 wallet balance, same as
a real signup). Change both before using the application anywhere public,
or pass `--email`, `--password`, `--test-email`, `--test-password` to
`seed_demo` to set your own.

The app uses a small demo 5sim catalog when `FIVESIM_API_KEY` is empty, so the
service browser can be explored immediately. Add a real key in the environment
or in Admin → Providers to use live inventory and phone numbers. Paystack
requires both `PAYSTACK_PUBLIC_KEY` and `PAYSTACK_SECRET_KEY`.

## Configuration

Copy `.env.example` to your environment and set:

- `DJANGO_SECRET_KEY`
- `ALLOWED_HOSTS`
- `FIVESIM_API_KEY`
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

## Main routes

`/`, `/login.html`, `/register.html`, `/user/dashboard.html`,
`/user/services.html`, `/user/cart.html`, `/user/topup.html`,
`/user/history.html`, `/user/settings.html`, `/user/sms.html`,
`/admin/dashboard.html`, `/admin/users.html`, `/admin/transactions.html`,
`/admin/providers.html`, `/admin/payment.html`, `/admin/coupons.html`, and
`/admin/settings.html`.

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
| `FIVESIM_API_KEY` | 5sim provider key for buying real phone numbers. Optional — empty means demo catalog. Can also be set later via `/admin/providers.html`. |

Optional (fallback only — prefer setting these in `/admin/payment.html` once deployed):

| Variable | Purpose |
|---|---|
| `PAYSTACK_PUBLIC_KEY` | Only used if no key has been saved in admin yet. |
| `PAYSTACK_SECRET_KEY` | Only used if no key has been saved in admin yet. |
