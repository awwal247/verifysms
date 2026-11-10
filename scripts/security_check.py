#!/usr/bin/env python3
"""Live, read-only security configuration checker (standard library only).

    python scripts/security_check.py https://verifysms.com --dkim-selectors default,selector1,google

Checks (all non-destructive, single GET/handshake requests against YOUR OWN site):
  headers   presence/values, duplicates, weak CSP tokens
  files     /.well-known/security.txt and /robots.txt answer 200 without redirects
  tls       TLS 1.2/1.3 accepted, 1.0/1.1 refused, certificate hostname/chain/expiry, negotiated cipher
  dns       SPF / DKIM / DMARC (needs the `dig` command; skipped when it is not installed)
OCSP stapling cannot be read with Python's ssl module: run
    openssl s_client -connect HOST:443 -servername HOST -status </dev/null | grep -A3 "OCSP response"
Exit status is 1 when any FAIL is reported.
"""
import argparse
import http.client
import re
import shutil
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

RESULTS = []


def report(level, area, message):
    RESULTS.append(level)
    print(f"[{level:4}] {area}: {message}")


# ── pure analysers (unit-testable, no network) ────────────────────────────
def analyze_csp(csp):
    problems = []
    if not csp:
        return ["missing"]
    if "'unsafe-eval'" in csp:
        problems.append("allows 'unsafe-eval'")
    if re.search(r"(script-src|default-src)[^;]*\s\*(\s|;|$)", csp):
        problems.append("wildcard script/default source")
    for needed in ("frame-ancestors", "object-src", "base-uri"):
        if needed not in csp:
            problems.append(f"no {needed}")
    return problems


def analyze_spf(txt_records):
    spf = [r.strip('"') for r in txt_records if r.strip('"').lower().startswith("v=spf1")]
    if not spf:
        return ["FAIL", "no SPF record"]
    if len(spf) > 1:
        return ["FAIL", "multiple SPF records (RFC 7208 allows exactly one)"]
    rec = spf[0]
    notes = []
    lookups = len(re.findall(r"\b(include:|a\b|mx\b|ptr\b|exists:|redirect=)", rec))
    if lookups > 10:
        notes.append(f"{lookups} DNS-lookup mechanisms (limit is 10)")
    if rec.rstrip().endswith("+all") or rec.rstrip().endswith(" all"):
        return ["FAIL", "ends in +all/all: anyone may send as this domain"]
    if rec.rstrip().endswith("?all"):
        notes.append("?all is neutral - move to ~all, then -all, once every sender is listed")
    elif rec.rstrip().endswith("~all"):
        notes.append("~all (softfail) - tighten to -all after reviewing DMARC reports")
    elif not rec.rstrip().endswith("-all"):
        notes.append("no terminating 'all' mechanism")
    return ["WARN" if notes else "PASS", "; ".join(notes) or "SPF ends in -all"]


def analyze_dmarc(txt_records):
    rec = [r.strip('"') for r in txt_records if r.strip('"').lower().startswith("v=dmarc1")]
    if not rec:
        return ["FAIL", "no DMARC record at _dmarc.<domain>"]
    tags = dict(t.strip().split("=", 1) for t in rec[0].split(";") if "=" in t)
    policy = tags.get("p", "").lower()
    notes = []
    if policy == "none":
        notes.append("p=none is monitoring only - move to quarantine, then reject")
    elif policy not in ("quarantine", "reject"):
        return ["FAIL", f"invalid p= value {policy!r}"]
    if "rua" not in tags:
        notes.append("no rua= aggregate-report address")
    return ["WARN" if notes else "PASS", "; ".join(notes) or f"p={policy} with reporting"]


# ── network checks ────────────────────────────────────────────────────────
def fetch(host, path, port=443, tls=True):
    cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
    conn = cls(host, port, timeout=10)
    conn.request("GET", path, headers={"User-Agent": "security-check/1.0"})
    resp = conn.getresponse()
    body = resp.read(65536).decode("utf-8", "replace")
    headers = resp.getheaders()
    conn.close()
    return resp.status, headers, body


def check_headers(host):
    status, headers, _ = fetch(host, "/login.html")
    seen = {}
    for k, v in headers:
        seen.setdefault(k.lower(), []).append(v)
    expect = {
        "strict-transport-security": lambda v: "max-age=" in v,
        "x-content-type-options": lambda v: v.lower() == "nosniff",
        "x-frame-options": lambda v: v.upper() in ("DENY", "SAMEORIGIN"),
        "referrer-policy": lambda v: v == "strict-origin-when-cross-origin",
        "permissions-policy": lambda v: "camera=()" in v and "microphone=()" in v and "geolocation=()" in v,
    }
    for name, ok in expect.items():
        vals = seen.get(name)
        if not vals:
            report("FAIL", "headers", f"{name} missing")
        elif len(vals) > 1:
            report("FAIL", "headers", f"{name} sent {len(vals)} times (conflict): {vals}")
        else:
            report("PASS" if ok(vals[0]) else "WARN", "headers", f"{name}: {vals[0][:90]}")
    csp = seen.get("content-security-policy", [""])
    problems = analyze_csp(csp[0])
    if len(csp) > 1:
        report("FAIL", "headers", "multiple Content-Security-Policy headers")
    elif problems == ["missing"]:
        report("FAIL", "headers", "content-security-policy missing")
    elif problems:
        report("WARN", "headers", "CSP: " + "; ".join(problems))
    else:
        report("PASS", "headers", "content-security-policy present")
    if "'unsafe-inline'" in csp[0]:
        report("INFO", "headers", "CSP still allows 'unsafe-inline' (inline handlers in templates) - see docs/SECURITY_HARDENING.md")
    if any(k in seen for k in ("x-powered-by",)):
        report("WARN", "headers", "x-powered-by discloses technology")


def check_files(host):
    for path in ("/.well-known/security.txt", "/robots.txt"):
        status, headers, body = fetch(host, path)
        ctype = dict((k.lower(), v) for k, v in headers).get("content-type", "")
        if status != 200:
            report("FAIL", "files", f"{path} -> HTTP {status} (expected 200 with no redirect)")
        elif not ctype.startswith("text/plain"):
            report("WARN", "files", f"{path} content-type {ctype!r}")
        else:
            report("PASS", "files", f"{path} -> 200 text/plain")
        if path.endswith("security.txt") and status == 200:
            if not re.search(r"(?m)^Contact:\s*\S+", body):
                report("FAIL", "files", "security.txt has no Contact field")
            m = re.search(r"(?m)^Expires:\s*(\S+)", body)
            if not m:
                report("FAIL", "files", "security.txt has no Expires field")
            else:
                exp = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                report("PASS" if exp > datetime.now(timezone.utc) else "FAIL", "files", f"security.txt Expires {m.group(1)}")
    status, headers, _ = fetch(host, "/", tls=False, port=80)
    loc = dict((k.lower(), v) for k, v in headers).get("location", "")
    report("PASS" if status in (301, 302, 307, 308) and loc.startswith("https://") else "FAIL",
           "tls", f"http://{host}/ -> HTTP {status} {loc}")


def try_tls(host, version):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ctx.maximum_version = version
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except (ValueError, ssl.SSLError):
        return "unsupported-locally"
    try:
        with socket.create_connection((host, 443), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            return "accepted:" + (t.cipher()[0])
    except (ssl.SSLError, OSError):
        return "refused"


def check_tls(host):
    for name, ver, want in (("TLS 1.0", ssl.TLSVersion.TLSv1, "refused"), ("TLS 1.1", ssl.TLSVersion.TLSv1_1, "refused"),
                            ("TLS 1.2", ssl.TLSVersion.TLSv1_2, "accepted"), ("TLS 1.3", ssl.TLSVersion.TLSv1_3, "accepted")):
        got = try_tls(host, ver)
        if got == "unsupported-locally":
            report("INFO", "tls", f"{name}: this Python/OpenSSL cannot test it - use `nmap --script ssl-enum-ciphers -p 443 {host}`")
        else:
            report("PASS" if got.startswith(want) else "FAIL", "tls", f"{name}: {got}")
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            cert = t.getpeercert()
            days = (datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
            report("PASS" if days > 14 else "WARN", "tls", f"certificate valid, hostname matches, chain trusted; {days} days left; issuer {dict(x[0] for x in cert['issuer']).get('organizationName')}")
            report("INFO", "tls", f"negotiated {t.version()} {t.cipher()[0]}; verify OCSP stapling with openssl s_client -status")
    except ssl.SSLCertVerificationError as exc:
        report("FAIL", "tls", f"certificate verification failed: {exc.verify_message}")


def dig(name, rtype="TXT"):
    out = subprocess.run(["dig", "+short", rtype, name], capture_output=True, text=True, timeout=15).stdout
    return [line for line in out.splitlines() if line.strip()]


def check_dns(domain, selectors):
    if not shutil.which("dig"):
        report("INFO", "dns", "`dig` not installed - skipping SPF/DKIM/DMARC checks")
        return
    level, msg = analyze_spf(dig(domain))
    report(level, "dns", f"SPF: {msg}")
    level, msg = analyze_dmarc(dig(f"_dmarc.{domain}"))
    report(level, "dns", f"DMARC: {msg}")
    found = [s for s in selectors if dig(f"{s}._domainkey.{domain}")]
    report("PASS" if found else "WARN", "dns", f"DKIM selectors found: {found or 'none of ' + ','.join(selectors)}")
    caa = dig(domain, "CAA")
    report("PASS" if caa else "INFO", "dns", "CAA: " + (caa[0] if caa else "none (optional: restrict which CAs may issue)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--dkim-selectors", default="default,selector1,selector2,google,k1,s1,mail")
    args = ap.parse_args()
    host = urlparse(args.url if "//" in args.url else "https://" + args.url).hostname
    for fn, arg in ((check_headers, host), (check_files, host), (check_tls, host)):
        try:
            fn(arg)
        except Exception as exc:  # network problems must not hide the other checks
            report("FAIL", fn.__name__, f"{type(exc).__name__}: {exc}")
    check_dns(host.removeprefix("www."), [s.strip() for s in args.dkim_selectors.split(",") if s.strip()])
    print(f"\n{RESULTS.count('PASS')} pass, {RESULTS.count('WARN')} warn, {RESULTS.count('FAIL')} fail")
    sys.exit(1 if "FAIL" in RESULTS else 0)


if __name__ == "__main__":
    main()
