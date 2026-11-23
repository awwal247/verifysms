#!/usr/bin/env python3
"""One entry point for every automated test, with a plain-text report.

    python scripts/run_tests.py                 # standalone always; full suite too when dependencies are installed
    python scripts/run_tests.py --standalone    # NO dependencies: bare Python 3.10+ (standard library only)
    python scripts/run_tests.py --full          # WITH dependencies: Django suite (pip install -r requirements.txt)
    python scripts/run_tests.py --live https://yourdomain.com   # + read-only live header/TLS/DNS probe
    python scripts/run_tests.py --report out.txt --verbose

Suites
  standalone  tests_standalone/   unittest only. Static analysis of source, templates, JS, settings text,
                                  migrations, routes, env docs, and the pure analysers in scripts/security_check.py.
  full        core/tests/         Django test runner. Needs Django, dj-database-url, psycopg. No network:
                                  provider and Paystack HTTP is mocked.
  live        scripts/security_check.py   Optional. Needs network access to YOUR site; never part of the default run.

Writes TEST_REPORT.txt (override with --report, disable with --no-report). Exit status: 0 all green,
1 a test failed, 2 a requested suite could not run (e.g. --full without Django installed).
"""
import argparse
import importlib.util
import os
import platform
import re
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / "TEST_REPORT.txt"
REQUIRED_MODULES = ("django", "dj_database_url", "psycopg")

# Dependency-free modules need nothing but the interpreter; dependency modules are described here so the
# report can say, per module, what it needs and what it proves.
MODULE_NOTES = OrderedDict([
    ("tests_standalone.test_security_check_script", "stdlib | CSP/SPF/DMARC analysers, CLI hygiene of scripts/security_check.py"),
    ("tests_standalone.test_frontend_static", "stdlib | JS/template XSS sinks, CSRF tokens, external hosts, referenced assets"),
    ("tests_standalone.test_project_integrity", "stdlib | syntax of every .py, secrets scan, .gitignore/.env.example, migrations chain, routes, docs"),
    ("tests_standalone.test_config_static", "stdlib | settings + middleware + views security posture read from source"),
    ("tests_standalone.test_runner", "stdlib | this runner's parser and report writer"),
    ("core.tests.test_security", "Django | headers, HSTS, security.txt, auth, throttling, CSRF, XSS regressions, seed_demo"),
    ("core.tests.test_verification", "Django | unit, functional, black-box, white-box, sanity: wallet, routing, coupons, Paystack, visibility"),
    ("core.tests.test_dependencies", "Django+deps | settings per environment, Postgres URL, migrations on blank DB, check --deploy, models, middleware, admin actions"),
])

RESULT_RE = re.compile(r"(?: \.\.\. |^)(ok|FAIL|ERROR|skipped .*|expected failure|unexpected success)\s*$")
NAME_RE = re.compile(r"(\w+) \(([\w.]+)\)")


# ── helpers (unit-tested in tests_standalone/test_runner.py) ──────────────
def missing_dependencies():
    """Names of required third-party modules that cannot be imported."""
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


def parse_unittest_output(text):
    """Parse ``-v 2`` unittest/Django output into ``[(module, class, test, status)]``.

    Handles docstring-style names where the result lands on the line after ``test_x (a.b.C.test_x)``,
    and log lines a test prints in between (``test_x (...) ... some log`` then ``ok`` on its own line).
    """
    results, pending = [], []
    for line in text.splitlines():
        if NAME_RE.search(line) and not RESULT_RE.search(line):
            pending = [line]
            continue
        match = RESULT_RE.search(line) if (pending or " ... " in line) else None
        if match:
            chunk = " ".join(pending + [line])
            found = NAME_RE.findall(chunk)
            if found:
                name, dotted = found[-1]
                parts = dotted.split(".")
                # dotted = package.module.Class.test_name
                cls = parts[-2] if len(parts) >= 2 else ""
                module = ".".join(parts[:-2]) if len(parts) > 2 else dotted
                status = match.group(1)
                status = "skipped" if status.startswith("skipped") else status
                results.append((module, cls, name, status))
            pending = []
        elif pending and line.strip():
            pending.append(line)
    return results


def summarize(results):
    """``{module: {class: {status: n}}}`` preserving first-seen order."""
    out = OrderedDict()
    for module, cls, _name, status in results:
        out.setdefault(module, OrderedDict()).setdefault(cls, OrderedDict())
        out[module][cls][status] = out[module][cls].get(status, 0) + 1
    return out


def totals(results):
    counts = {"ok": 0, "FAIL": 0, "ERROR": 0, "skipped": 0, "expected failure": 0, "unexpected success": 0}
    for *_ , status in results:
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(results)
    return counts


def verdict(counts):
    return "PASS" if counts["FAIL"] == 0 and counts["ERROR"] == 0 and counts["unexpected success"] == 0 else "FAIL"


def render_report(meta, suites):
    """Plain-text report. ``suites`` is a list of dicts: name, status, results, seconds, note, output."""
    line = "=" * 78
    thin = "-" * 78
    out = [line, "VerifySMS  -  TEST REPORT", line,
           f"Generated : {meta['when']}",
           f"Python    : {meta['python']}  ({meta['platform']})",
           f"Django    : {meta['django']}",
           f"Command   : {meta['command']}",
           "", "SUMMARY", thin,
           f"{'Suite':<14}{'Result':<10}{'Tests':>7}{'Passed':>8}{'Failed':>8}{'Errors':>8}{'Skipped':>9}{'Time':>9}", thin]
    overall_fail = overall_run = overall_pass = 0
    for s in suites:
        c = totals(s["results"])
        if s["status"] == "SKIPPED":
            out.append(f"{s['name']:<14}{'SKIPPED':<10}{'-':>7}{'-':>8}{'-':>8}{'-':>8}{'-':>9}{'-':>9}")
            continue
        res = verdict(c) if s["status"] != "CRASH" else "CRASH"
        out.append(f"{s['name']:<14}{res:<10}{c['total']:>7}{c['ok']:>8}{c['FAIL']:>8}{c['ERROR']:>8}{c['skipped']:>9}{s['seconds']:>8.1f}s")
        overall_run += c["total"]; overall_pass += c["ok"]; overall_fail += c["FAIL"] + c["ERROR"]
        if res != "PASS":
            overall_fail = max(overall_fail, 1)
    out += [thin, f"{'TOTAL':<14}{('PASS' if overall_fail == 0 and not any(s['status'] == 'CRASH' for s in suites) else 'FAIL'):<10}"
            f"{overall_run:>7}{overall_pass:>8}", ""]

    out += ["DEPENDENCY MODES", thin,
            "standalone : needs only Python. Run it anywhere (fresh clone, CI before `pip install`, locked-down box).",
            "full       : needs `pip install -r requirements.txt` (Django, dj-database-url, psycopg).",
            "live       : optional, needs network access to a deployed site (scripts/security_check.py).", ""]

    for s in suites:
        out += [f"SUITE: {s['name']}", thin]
        if s["status"] == "SKIPPED":
            out += [f"SKIPPED - {s['note']}", ""]
            continue
        if s["status"] == "CRASH":
            out += ["The runner crashed before reporting results:", s["output"][-2000:], ""]
            continue
        for module, classes in summarize(s["results"]).items():
            n = sum(sum(v.values()) for v in classes.values())
            bad = sum(v.get("FAIL", 0) + v.get("ERROR", 0) for v in classes.values())
            out.append(f"{module}  [{n} tests, {'all passed' if not bad else str(bad) + ' failing'}]")
            note = MODULE_NOTES.get(module)
            if note:
                out.append(f"    needs/proves: {note}")
            for cls, counts in classes.items():
                flag = "" if not (counts.get("FAIL") or counts.get("ERROR")) else "   <-- FAILING"
                out.append(f"    {cls:<38}{sum(counts.values()):>4}{flag}")
        failures = [r for r in s["results"] if r[3] in ("FAIL", "ERROR", "unexpected success")]
        if failures:
            out += ["", "FAILURES"]
            out += [f"  [{st}] {mod}.{cls}.{name}" for mod, cls, name, st in failures]
            out += ["", "Details:", s["output"][s["output"].find("=" * 70):][:6000] if "=" * 70 in s["output"] else s["output"][-3000:]]
        out.append("")

    out += ["KNOWN OPEN ITEMS (found while building the suites)", thin,
            "1. (resolved) Tutorial videos and posters are now in assets/video/; KNOWN_MISSING_MEDIA is empty.",
            "2. CSP still allows 'unsafe-inline' (templates use inline on*= handlers and style= attributes).",
            "   See docs/SECURITY_HARDENING.md section 3 for the removal path.",
            "3. Live TLS / DNS (SPF, DKIM, DMARC, OCSP) cannot be proven offline: use --live after deploying.",
            "", "NOT COVERED BY AUTOMATED TESTS", thin,
            "Real SMS-BUS / 5SIM / Paystack calls (mocked on purpose), browser rendering and JS behaviour,",
            "Postgres-specific locking (tests run on SQLite), load/performance, and the Vercel edge.", "", line]
    return "\n".join(out) + "\n"


# ── execution ─────────────────────────────────────────────────────────────
def _run(cmd, env=None, timeout=1800):
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    return proc, time.monotonic() - started


def run_standalone(verbose):
    cmd = [sys.executable, "-I", "-m", "unittest", "discover", "-s", "tests_standalone", "-t", ".", "-v"]
    proc, secs = _run(cmd)
    text = proc.stdout + "\n" + proc.stderr
    results = parse_unittest_output(text)
    if verbose:
        print(text)
    return {"name": "standalone", "status": "RAN", "results": results, "seconds": secs, "output": text,
            "returncode": proc.returncode, "note": ""}


def run_full(verbose):
    missing = missing_dependencies()
    if missing:
        return {"name": "full", "status": "SKIPPED", "results": [], "seconds": 0, "output": "", "returncode": 2,
                "note": f"missing modules: {', '.join(missing)}. Run: pip install -r requirements.txt"}
    env = dict(os.environ)
    if not env.get("DJANGO_DEBUG") and not env.get("DJANGO_SECRET_KEY"):
        env["DJANGO_DEBUG"] = "1"   # settings refuse to load otherwise (see README)
    proc, secs = _run([sys.executable, "manage.py", "test", "core", "-v", "2", "--noinput"], env=env)
    text = proc.stdout + "\n" + proc.stderr
    results = parse_unittest_output(text)
    if verbose:
        print(text)
    status = "RAN" if results else "CRASH"
    return {"name": "full", "status": status, "results": results, "seconds": secs, "output": text,
            "returncode": proc.returncode, "note": ""}


def run_live(url, verbose):
    proc, secs = _run([sys.executable, str(ROOT / "scripts" / "security_check.py"), url], timeout=300)
    text = proc.stdout + proc.stderr
    if verbose:
        print(text)
    lines = [l for l in text.splitlines() if l.startswith("[")]
    results = [("live.security_check", "Probe", l[7:].split(":")[0] + ": " + l.split(": ", 1)[-1][:50],
                "FAIL" if l.startswith("[FAIL") else "ok") for l in lines]
    return {"name": "live", "status": "RAN", "results": results, "seconds": secs, "output": text,
            "returncode": proc.returncode, "note": ""}


def django_version():
    try:
        import django
        return django.get_version()
    except ImportError:
        return "not installed"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--standalone", action="store_true", help="dependency-free suite only")
    ap.add_argument("--full", action="store_true", help="Django suite only (needs requirements installed)")
    ap.add_argument("--live", metavar="URL", help="also run the read-only live probe against URL")
    ap.add_argument("--report", default=str(DEFAULT_REPORT), help="report path (default TEST_REPORT.txt)")
    ap.add_argument("--no-report", action="store_true", help="do not write a report file")
    ap.add_argument("--verbose", "-v", action="store_true", help="print raw test output")
    args = ap.parse_args(argv)

    suites, explicit_full = [], args.full
    if args.standalone or not args.full:
        suites.append(run_standalone(args.verbose))
    if args.full or not args.standalone:
        suites.append(run_full(args.verbose))
    if args.live:
        suites.append(run_live(args.live, args.verbose))

    meta = {"when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "python": platform.python_version(),
            "platform": platform.platform(), "django": django_version(),
            "command": "python scripts/run_tests.py " + " ".join(argv if argv is not None else sys.argv[1:])}
    report = render_report(meta, suites)
    print(report)
    if not args.no_report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"Report written to {args.report}")

    failed = any(s["status"] in ("RAN", "CRASH") and (s["status"] == "CRASH" or verdict(totals(s["results"])) == "FAIL"
                 or s["returncode"] not in (0, None) and s["name"] != "live") for s in suites)
    skipped_required = explicit_full and any(s["status"] == "SKIPPED" for s in suites)
    return 1 if failed else 2 if skipped_required else 0


if __name__ == "__main__":
    sys.exit(main())
