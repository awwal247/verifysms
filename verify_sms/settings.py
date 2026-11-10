"""VerifySMS Django settings.

The project uses Supabase Postgres when DATABASE_URL is set, and falls back
to local SQLite when it isn't — so it can still be unpacked and run without
provisioning a database. Provider credentials are read from environment
variables and are never committed to the project.
"""
import os
import dj_database_url
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Secure by default: debug must be switched ON explicitly (DJANGO_DEBUG=1 for local dev).
DEBUG = _flag("DJANGO_DEBUG", "0")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-change-me"  # local development only
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"` and set it as an "
            "environment variable (never commit it). For local development set DJANGO_DEBUG=1."
        )

SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
SITE_IS_HTTPS = SITE_URL.lower().startswith("https://")
# True only when we are certain the public site is served over HTTPS.
HTTPS_ENFORCED = SITE_IS_HTTPS and not DEBUG


def _allowed_hosts():
    configured = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if DEBUG:
        return configured or ["127.0.0.1", "localhost", "[::1]"]
    # In production a wildcard defeats Host-header validation. Replace it with the
    # hosts we can derive (SITE_URL, its www/apex twin and Vercel's own hostnames).
    configured = [h for h in configured if h != "*"]
    derived = []
    site_host = urlparse(SITE_URL).hostname
    if site_host:
        derived.append(site_host)
        derived.append(site_host[4:] if site_host.startswith("www.") else "www." + site_host)
    for var in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL"):
        host = os.getenv(var, "").strip()
        if host:
            derived.append(host)
    hosts = list(dict.fromkeys(configured + derived))
    if not hosts:
        raise ImproperlyConfigured("Set ALLOWED_HOSTS (comma-separated domains) or SITE_URL.")
    return hosts


ALLOWED_HOSTS = _allowed_hosts()
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
if SITE_IS_HTTPS and SITE_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(SITE_URL)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.ActiveUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "verify_sms.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.csrf",
                "core.context_processors.site_context",
            ],
        },
    }
]
WSGI_APPLICATION = "verify_sms.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL:
    # Supabase Postgres (or any Postgres URL). Supabase's pooler connection
    # string requires SSL, so it's enforced here even if the URL omits it.
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "60")),
            ssl_require=os.getenv("DB_SSL_REQUIRE", "1").lower() in {"1", "true", "yes"},
        )
    }
else:
    # Local dev fallback: no DATABASE_URL means no Postgres provisioned yet.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "core.User"
LOGIN_URL = "/login.html"
LOGIN_REDIRECT_URL = "/user/dashboard.html"
LOGOUT_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/assets/"
STATICFILES_DIRS = [BASE_DIR / "assets"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Only used by the one-time /deploy-setup/ endpoint (see core/views.py).
# Leave unset in normal operation; set it temporarily in Vercel's env vars
# only while you need to trigger a remote migrate+seed, then remove it.
DEPLOY_SETUP_TOKEN = os.getenv("DEPLOY_SETUP_TOKEN", "")
FIVESIM_API_KEY = os.getenv("FIVESIM_API_KEY", "")
SMSBUS_API_KEY = os.getenv("SMSBUS_API_KEY", "")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")

# ── Authentication ────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Sessions & cookies ────────────────────────────────────────────────────
SESSION_COOKIE_NAME = "vsms_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = HTTPS_ENFORCED
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", str(7 * 24 * 3600)))
# The CSRF cookie must stay readable by JS: assets/js/app.js copies it into X-CSRFToken.
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = HTTPS_ENFORCED

# ── Transport security & headers (emitted by Django's SecurityMiddleware) ──
# Vercel terminates TLS and overwrites X-Forwarded-Proto, so it is safe to trust there.
if _flag("TRUST_PROXY_SSL_HEADER", "1" if os.getenv("VERCEL") else "0"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = HTTPS_ENFORCED and _flag("SECURE_SSL_REDIRECT", "1")
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
# HSTS ramp-up: starts at 1 day. Once every page/subdomain is confirmed HTTPS-only, raise
# SECURE_HSTS_SECONDS to 31536000, then consider INCLUDE_SUBDOMAINS / PRELOAD.
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "86400")) if HTTPS_ENFORCED else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _flag("SECURE_HSTS_INCLUDE_SUBDOMAINS", "0")
SECURE_HSTS_PRELOAD = _flag("SECURE_HSTS_PRELOAD", "0")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# allow-popups keeps Paystack's checkout/3-D Secure windows working.
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"
X_FRAME_OPTIONS = "DENY"

# CSP is built in core/middleware.py. Set CSP_REPORT_ONLY=1 to observe without enforcing.
CSP_REPORT_ONLY = _flag("CSP_REPORT_ONLY", "0")
CSP_REPORT_URI = os.getenv("CSP_REPORT_URI", "")

# ── Request size limits ───────────────────────────────────────────────────
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024  # no file uploads exist in this app
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

# ── security.txt (RFC 9116) ───────────────────────────────────────────────
# A mailto: or https: URI where researchers can reach you. Falls back to the support
# WhatsApp link if unset — set a monitored mailbox, e.g. mailto:security@yourdomain.com
SECURITY_CONTACT = os.getenv("SECURITY_CONTACT", "").strip()

# ── Caching (login/registration throttles) ────────────────────────────────
# A shared cache makes the throttles global across serverless instances.
# Set REDIS_URL (and add `redis>=5` to requirements.txt) to enable it.
if os.getenv("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.environ["REDIS_URL"]}}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"core.security": {"handlers": ["console"], "level": "INFO"}},
}