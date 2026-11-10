# VerifySMS – security hardening report & runbook

Scope: static review of the uploaded project (Django 5.2, deployed on Vercel, Supabase/SQLite).
No live site, DNS zone or hosting dashboard was accessible, so TLS and email items below are
**runbooks to execute and verify**, not verified facts. Nothing here makes the site "completely secure".

## 1. Architecture facts that drove the design
* No `vercel.json`, nginx or CDN config exists in the repo → **headers are owned by Django**
  (`SecurityMiddleware` + `core/middleware.py`). Do not also add the same headers in `vercel.json`
  or Cloudflare; if you must, delete the Django side first. Middleware never overwrites a header a view set.
* Files that Vercel serves straight from its CDN (not through Django) do not pass the middleware.
  Run `scripts/security_check.py` – if `/assets/*` lacks headers, add a `headers` block in `vercel.json` for `/assets/(.*)` only.
* The app sends **no email** (no SMTP/ESP code) – SPF/DKIM/DMARC concern only whatever mailboxes/newsletters use the domain.

## 2. Change log (every file touched)
| File | Change |
|---|---|
| `verify_sms/settings.py` | Debug off by default; refuses to start without `DJANGO_SECRET_KEY`; `*` removed from `ALLOWED_HOSTS` in prod (derived from `SITE_URL`/Vercel); `Secure` session+CSRF cookies; HTTPS redirect, proxy header, HSTS (staged, default 1 day), nosniff, Referrer-Policy, COOP, `X_FRAME_OPTIONS=DENY`; password validators; 7-day sessions; request-size limits; optional shared cache; security logger |
| `core/middleware.py` (new) | CSP (or Report-Only), Permissions-Policy, `no-store` + `noindex` on private paths; `ActiveUserMiddleware` ends banned users' sessions incl. JSON APIs |
| `core/well_known.py` (new), `core/urls.py` | `/.well-known/security.txt`, `/security.txt`, `/robots.txt` served directly (200, text/plain, no redirect, no login/maintenance gate); `Expires` computed at request time |
| `core/views.py` | Open-redirect fix on login; login/register/password-change throttling; password policy; **current password now verified** on change; `update_session_auth_hash`; coupon row-lock; atomic refund (no double refund); Paystack verify binds reference+currency and never downgrades a credited top-up; webhook JSON errors → 400; callback reference URL-encoded; `deploy_setup` 404/constant-time/never seeds default accounts in prod; promo-slide and support links restricted to safe URLs |
| `core/services.py` | `process_topup` serialised with a user-row lock (webhook + verify race could double-credit) |
| `assets/js/app.js` | Stored-XSS fixes: `showToast` and `updateOrderCard` no longer render provider/SMS text as HTML; delegated `[data-copy]` handler |
| `templates/user/sms.html`, `templates/user/cart.html` | Removed `onclick="copyText('…')"` built from SMS code (apostrophe broke out of the JS string → XSS) |
| `.gitignore`, `.env.example` | Ignore DB/pyc/venv; document all new variables |
| `core/tests/test_security.py`, `scripts/security_check.py` (new) | Regression tests; live header/TLS/DNS checker |

## 3. Headers – CSP rationale
Enforced policy: `default-src 'self'`; scripts `'self' 'unsafe-inline'` + cdnjs + js.paystack.co; styles `'self' 'unsafe-inline'` + cdnjs;
fonts cdnjs/data; images `'self' data: https:`; connect `'self'` + api.paystack.co; frames only checkout.paystack.com;
`object-src 'none'`, `base-uri 'self'`, `form-action 'self'`, `frame-ancestors 'none'`, `upgrade-insecure-requests` on HTTPS. No `unsafe-eval`.
**`'unsafe-inline'` is technically necessary today**: templates contain ~140 inline `on*=` handlers, ~230 `style=""` attributes and 6 inline `<script>` blocks; nonces cannot authorise inline handlers.
Path to remove it: (1) move handlers to `addEventListener` in external JS, (2) move inline scripts/styles to files, (3) test with `CSP_REPORT_ONLY=1`, (4) drop `'unsafe-inline'`.
HSTS: starts at 86 400 s. After a week of clean operation set `SECURE_HSTS_SECONDS=31536000`; enable `INCLUDE_SUBDOMAINS` only after every subdomain is HTTPS; `PRELOAD` is effectively irreversible.

## 4. TLS (hosting-controlled – verify, don't assume)
Vercel terminates TLS and manages certificates (auto-renewing). Verify with:
```
python scripts/security_check.py https://YOURDOMAIN
nmap --script ssl-enum-ciphers -p 443 YOURDOMAIN          # TLS1.0/1.1 absent, no CBC-SHA1/3DES/RC4/EXPORT
openssl s_client -connect YOURDOMAIN:443 -servername YOURDOMAIN -status </dev/null | grep -A3 "OCSP response"
```
Cipher suites/protocol versions/OCSP stapling cannot be changed from app code on Vercel; if the checks fail, open a support ticket
or front the site with Cloudflare (SSL → Minimum TLS 1.2, Always Use HTTPS). Certificate checks: hostname match, full chain served,
renewal ≥ 14 days out, add a **CAA** record (`0 issue "letsencrypt.org"` – match the CA your host actually uses).

## 5. Email (DNS – nothing was overwritten; zone was not accessible)
1. Inventory senders: mailbox provider (Google/Microsoft/Zoho…), newsletter/support/billing tools, any "send as" service. Use DMARC `rua` reports for a week to find forgotten ones.
2. **SPF** – exactly one TXT at the apex, ≤10 lookups. Examples (use only what you actually use):
   * domain sends no mail: `v=spf1 -all`
   * Google Workspace: `v=spf1 include:_spf.google.com ~all` → change `~all` to `-all` once DMARC reports are clean.
   Merge into the existing record; never publish a second `v=spf1`.
3. **DKIM** – enable signing in each sending service and publish the CNAME/TXT it gives you at `<selector>._domainkey`. `--dkim-selectors` in the checker probes common selectors.
4. **DMARC** – `_dmarc` TXT: start `v=DMARC1; p=none; rua=mailto:dmarc@YOURDOMAIN; fo=1`, then `quarantine; pct=25` → `100` → `reject` over weeks. Domain that sends nothing: `v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s`.

## 6. Security files
`/.well-known/security.txt` now returns 200 with `Contact`, rolling `Expires` (180 days), `Preferred-Languages`, `Canonical`.
**Set `SECURITY_CONTACT=mailto:…`** – until then it falls back to the support WhatsApp link. The `/robots.txt` redirect could not be reproduced
(no routing config in repo; Django had no route so it could only 404). It is now an explicit route; if the live site still redirects, the cause is outside this code (Vercel redirect/rewrite rule, domain-level redirect www↔apex, or Cloudflare rule) – check `curl -sI https://DOMAIN/robots.txt`.

## 7. Tests
* `DJANGO_DEBUG=1 python manage.py test core` – 40-ish assertions (headers, HSTS/redirect, security.txt/robots, open redirect, throttling, password change, ban enforcement, CSRF, deploy-setup, URL sanitising, XSS regressions, single top-up credit).
* `python scripts/security_check.py https://YOURDOMAIN` – live checks (read-only).
