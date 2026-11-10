"""Security middleware for VerifySMS.

Header ownership (so nothing is sent twice):

  Django's SecurityMiddleware  -> Strict-Transport-Security, X-Content-Type-Options,
                                  Referrer-Policy, Cross-Origin-Opener-Policy, HTTPS redirect
  XFrameOptionsMiddleware      -> X-Frame-Options
  SecurityHeadersMiddleware    -> Content-Security-Policy (or -Report-Only),
  (this file)                     Permissions-Policy, Cache-Control for private pages
  ActiveUserMiddleware         -> ends sessions of banned/suspended accounts

The hosting layer (Vercel) must NOT add the same headers in vercel.json; if it
does, remove one side. ``_set_once`` never overwrites a header a view already set.
"""
from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect

# Third parties the pages actually load (audited from templates/ and assets/):
#   cdnjs.cloudflare.com  Font Awesome CSS + webfonts
#   js.paystack.co        Paystack inline.js (topup page)
#   checkout.paystack.com / api.paystack.co  Paystack checkout iframe + API
CDNJS = "https://cdnjs.cloudflare.com"
PAYSTACK_SCRIPT = "https://js.paystack.co"
PAYSTACK_FRAME = "https://checkout.paystack.com"
PAYSTACK_API = "https://api.paystack.co"

PERMISSIONS_POLICY = ", ".join([
    "camera=()", "microphone=()", "geolocation=()", "usb=()", "serial=()",
    "bluetooth=()", "magnetometer=()", "gyroscope=()", "accelerometer=()",
    "midi=()", "display-capture=()",
    # Paystack's checkout iframe may use the Payment Request API.
    f'payment=(self "{PAYSTACK_FRAME}")',
])

# Pages/APIs that contain per-user data: never store in browser or shared caches.
PRIVATE_PREFIXES = ("/user/", "/admin/", "/api/", "/login.html", "/register.html", "/django-admin/")


def build_csp(upgrade_insecure_requests=False, report_uri=""):
    """Return the Content-Security-Policy string.

    NOTE on 'unsafe-inline': the templates still contain ~140 inline event
    handlers (onclick=...), ~230 style="" attributes and a handful of inline
    <script>/<style> blocks. A nonce cannot cover inline *handlers*, so removing
    'unsafe-inline' today would break the UI. Everything else is locked down
    (no 'unsafe-eval', no wildcard script hosts, no plugins, no framing, no
    base-tag/form hijacking). Migration path: docs/SECURITY_HARDENING.md.
    """
    directives = [
        "default-src 'self'",
        f"script-src 'self' 'unsafe-inline' {CDNJS} {PAYSTACK_SCRIPT}",
        f"style-src 'self' 'unsafe-inline' {CDNJS}",
        f"font-src 'self' data: {CDNJS}",
        "img-src 'self' data: https:",
        "media-src 'self'",
        f"connect-src 'self' {PAYSTACK_API} {PAYSTACK_FRAME}",
        f"frame-src {PAYSTACK_FRAME}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "manifest-src 'self'",
        "worker-src 'self'",
    ]
    if upgrade_insecure_requests:
        directives.append("upgrade-insecure-requests")
    if report_uri:
        directives.append(f"report-uri {report_uri}")
    return "; ".join(directives)


def _set_once(response, header, value):
    if header not in response:
        response[header] = value


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        https = getattr(settings, "SITE_IS_HTTPS", False)
        self.csp = build_csp(
            upgrade_insecure_requests=https and not settings.DEBUG,
            report_uri=getattr(settings, "CSP_REPORT_URI", ""),
        )
        # CSP_REPORT_ONLY=1 lets you watch for violations without enforcing.
        self.csp_header = (
            "Content-Security-Policy-Report-Only"
            if getattr(settings, "CSP_REPORT_ONLY", False)
            else "Content-Security-Policy"
        )

    def __call__(self, request):
        response = self.get_response(request)
        _set_once(response, self.csp_header, self.csp)
        _set_once(response, "Permissions-Policy", PERMISSIONS_POLICY)
        if request.path.startswith(PRIVATE_PREFIXES):
            _set_once(response, "Cache-Control", "no-store, max-age=0")
            # Keeps admin/user/API URLs out of search indexes without listing them in robots.txt.
            _set_once(response, "X-Robots-Tag", "noindex, nofollow")
        return response


class ActiveUserMiddleware:
    """A ban/suspension must take effect immediately, including on JSON APIs.

    Previously only the HTML ``/user/*.html`` views re-checked ``status``; a banned
    user with a live session could still call /api/orders/ and spend their wallet.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "status", "active") != "active":
            logout(request)
            if request.path.startswith("/api/") or request.path == "/admin/action/":
                return JsonResponse({"success": False, "message": "This account is not active."}, status=403)
            return redirect("/login.html?error=banned")
        return self.get_response(request)
