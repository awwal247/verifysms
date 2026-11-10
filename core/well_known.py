"""Plain-text security metadata endpoints: /.well-known/security.txt (RFC 9116) and /robots.txt.

Served by Django (not static files) so that:
  * they always answer 200 text/plain directly - no redirects, no login/maintenance gate;
  * they pass through the same security-header middleware as every other response;
  * security.txt can never go stale: ``Expires`` is computed relative to the current time.
"""
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_safe

from .models import Setting

EXPIRY_DAYS = 180


def security_contact():
    """Contact URI for researchers: SECURITY_CONTACT env, else the support WhatsApp link."""
    configured = getattr(settings, "SECURITY_CONTACT", "")
    if configured and re.match(r"^(mailto:[^\s@]+@[^\s@]+\.[^\s@]+|https://[^\s]+)$", configured):
        return configured
    digits = re.sub(r"[^0-9]", "", Setting.value("support_whatsapp", "2348086218152"))
    return f"https://wa.me/{digits}" if digits else ""


def build_security_txt(now=None):
    now = now or timezone.now()
    expires = (now + timedelta(days=EXPIRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    lines = []
    contact = security_contact()
    if contact:
        lines.append(f"Contact: {contact}")
    lines.append(f"Expires: {expires}")
    lines.append("Preferred-Languages: en")
    site = settings.SITE_URL
    if urlparse(site).scheme == "https":  # RFC 9116: Canonical must be https
        lines.append(f"Canonical: {site}/.well-known/security.txt")
    return "\n".join(lines) + "\n"


def build_robots_txt():
    # Sensitive areas are protected by authentication and X-Robots-Tag; robots.txt only
    # asks well-behaved crawlers to skip app/API paths. It is not an access control.
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /user/",
        "Disallow: /admin/",
        "Disallow: /api/",
        "",
    ])


def _text(body):
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_safe
def security_txt(request):
    return _text(build_security_txt())


@require_safe
def robots_txt(request):
    return _text(build_robots_txt())
