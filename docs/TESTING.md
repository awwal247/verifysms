# Testing guide

Two suites, split by what they need installed. Both are plain `unittest`; neither touches the network.

| Suite | Location | Needs | Typical use |
|---|---|---|---|
| **Standalone** | `tests_standalone/` | Python 3.10+ only (standard library) | Fresh clone, CI step before `pip install`, quick pre-commit check |
| **Full** | `core/tests/` | `pip install -r requirements.txt` (Django, dj-database-url, psycopg) | Behaviour of the running app: HTTP, database, wallet, providers, Paystack |
| **Live** (optional) | `scripts/security_check.py` | Network access to your deployed site | After each deploy: headers, TLS, DNS |

## Run everything

```bash
python scripts/run_tests.py                  # standalone, plus full when dependencies are installed
python scripts/run_tests.py --standalone     # no dependencies
python scripts/run_tests.py --full           # with dependencies (exit 2 if they are missing)
python scripts/run_tests.py --live https://yourdomain.com
python scripts/run_tests.py --report out.txt # default: TEST_REPORT.txt
```

The runner writes **`TEST_REPORT.txt`** (summary table, per-module breakdown, failures, known open items,
what is not covered). Exit status: `0` green, `1` a test failed, `2` a requested suite could not run.
Commit the report if you want a record of the last known-good run; regenerate it after any change.

Running a suite directly:

```bash
python -I -m unittest discover -s tests_standalone -t . -v
DJANGO_DEBUG=1 python manage.py test core -v 2                       # all Django tests
DJANGO_DEBUG=1 python manage.py test core.tests.test_dependencies    # one module
```

`DJANGO_DEBUG=1` (or a `DJANGO_SECRET_KEY`) is required, otherwise settings refuse to load by design.

## What each module covers

### Standalone (no dependencies)
| Module | Covers |
|---|---|
| `test_security_check_script` | CSP / SPF / DMARC analysers, report output, stdlib-only guarantee of the live checker |
| `test_frontend_static` | No `eval`/`document.write`, provider/SMS text never interpolated into HTML, CSRF token on every POST form, `target=_blank` has `noopener`, external script hosts allow-listed, referenced assets exist |
| `test_project_integrity` | Every `.py` parses, secret-pattern scan, `.gitignore` / `.env.example` hygiene, every env var the code reads is documented, bounded requirements, migration chain, every routed view exists, state-changing views restrict methods, docs present |
| `test_config_static` | Cookie/header/HSTS/CSRF settings, secret-key refusal, wildcard-host removal, middleware order, CSP directives and host allow-list, no raw SQL / `mark_safe(request…)`, constant-time token compare, atomic wallet code |
| `test_runner` | The report parser and writer in `scripts/run_tests.py` |

### Full (with dependencies)
| Module | Covers |
|---|---|
| `test_security` | Response headers, HSTS + redirect, `security.txt`/`robots.txt`, open-redirect, login throttling, password policy, ban enforcement, CSRF, deploy-setup, XSS regressions, `seed_demo` safety |
| `test_verification` | Unit (money, pricing, wallet, errors, Paystack, SMS-BUS client, routing/fallback, coupons), functional (auth, purchase/refund, Paystack init/verify/webhook), black-box authorization, white-box failure paths, smoke, number/code visibility |
| `test_dependencies` | `settings.py` under different environments (prod without key, HTTPS flags, derived hosts, Vercel, proxy header, SQLite fallback, **Postgres `DATABASE_URL` with SSL**, Redis cache) run in clean subprocesses; migrations on a blank DB; `check --deploy`; `manage.py check`; WSGI entrypoint; models and manager; middleware; service helpers; admin actions (credit/debit/ban/settings/coupons, secrets never echoed); maintenance mode; 404 handling; every template compiles |

## Conventions
* No test may call a real provider or Paystack. Mock at the adapter boundary (`core.services.urlopen`, whole adapters).
* A test that needs a different environment runs `python -c …` in a subprocess (see `run_python` in `test_dependencies.py`) so the test process keeps its own settings.
* Anything that can be proven by reading source belongs in `tests_standalone/` so it runs without installing anything.
* Known gaps are allow-listed explicitly (e.g. `KNOWN_MISSING_MEDIA`) and a companion test fails when the list goes stale.

## Not covered
Real SMS-BUS / 5SIM / Paystack responses, browser rendering and JavaScript behaviour, Postgres-specific row locking
(tests run on SQLite), load testing, and the Vercel edge (use `--live` after deploying).
