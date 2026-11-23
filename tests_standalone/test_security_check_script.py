"""scripts/security_check.py pure analysers - no network, no Django."""
import unittest
from pathlib import Path

from tests_standalone._common import load_module

sc = load_module("scripts/security_check.py", "security_check")

GOOD_CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


class CspAnalyser(unittest.TestCase):
    def test_missing_csp(self):
        self.assertEqual(sc.analyze_csp(""), ["missing"])
        self.assertEqual(sc.analyze_csp(None), ["missing"])

    def test_good_policy_has_no_problems(self):
        self.assertEqual(sc.analyze_csp(GOOD_CSP), [])

    def test_unsafe_eval_flagged(self):
        self.assertIn("allows 'unsafe-eval'", sc.analyze_csp(GOOD_CSP + "; script-src 'unsafe-eval'"))

    def test_wildcard_script_source_flagged(self):
        self.assertIn("wildcard script/default source", sc.analyze_csp("default-src * ; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"))

    def test_missing_hardening_directives_flagged(self):
        problems = sc.analyze_csp("default-src 'self'")
        for needed in ("frame-ancestors", "object-src", "base-uri"):
            self.assertIn(f"no {needed}", problems)


class SpfAnalyser(unittest.TestCase):
    def test_missing(self):
        self.assertEqual(sc.analyze_spf([])[0], "FAIL")
        self.assertEqual(sc.analyze_spf(['"google-site-verification=x"'])[0], "FAIL")

    def test_multiple_records_fail(self):
        level, msg = sc.analyze_spf(['"v=spf1 -all"', '"v=spf1 ~all"'])
        self.assertEqual(level, "FAIL")
        self.assertIn("multiple", msg)

    def test_hardfail_passes(self):
        self.assertEqual(sc.analyze_spf(['"v=spf1 include:_spf.google.com -all"'])[0], "PASS")

    def test_softfail_and_neutral_warn(self):
        self.assertEqual(sc.analyze_spf(['"v=spf1 include:_spf.google.com ~all"'])[0], "WARN")
        self.assertEqual(sc.analyze_spf(['"v=spf1 include:_spf.google.com ?all"'])[0], "WARN")

    def test_plus_all_fails(self):
        self.assertEqual(sc.analyze_spf(['"v=spf1 +all"'])[0], "FAIL")

    def test_no_terminating_all_warns(self):
        self.assertEqual(sc.analyze_spf(['"v=spf1 include:_spf.google.com"'])[0], "WARN")

    def test_lookup_limit_warns(self):
        rec = '"v=spf1 ' + " ".join(f"include:s{i}.example" for i in range(11)) + ' -all"'
        level, msg = sc.analyze_spf([rec])
        self.assertEqual(level, "WARN")
        self.assertIn("limit is 10", msg)

    def test_case_insensitive_version_tag(self):
        self.assertEqual(sc.analyze_spf(['"V=SPF1 -all"'])[0], "PASS")


class DmarcAnalyser(unittest.TestCase):
    def test_missing(self):
        self.assertEqual(sc.analyze_dmarc([])[0], "FAIL")

    def test_reject_with_reports_passes(self):
        self.assertEqual(sc.analyze_dmarc(['"v=DMARC1; p=reject; rua=mailto:d@example.com"'])[0], "PASS")

    def test_none_policy_warns(self):
        level, msg = sc.analyze_dmarc(['"v=DMARC1; p=none; rua=mailto:d@example.com"'])
        self.assertEqual(level, "WARN")
        self.assertIn("monitoring only", msg)

    def test_missing_rua_warns(self):
        level, msg = sc.analyze_dmarc(['"v=DMARC1; p=quarantine"'])
        self.assertEqual(level, "WARN")
        self.assertIn("rua", msg)

    def test_invalid_policy_fails(self):
        self.assertEqual(sc.analyze_dmarc(['"v=DMARC1; p=banana"'])[0], "FAIL")


class ReportingAndCli(unittest.TestCase):
    def test_report_records_level_and_prints(self):
        sc.RESULTS.clear()
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.report("PASS", "unit", "hello")
        self.assertIn("[PASS] unit: hello", buf.getvalue())
        self.assertEqual(sc.RESULTS, ["PASS"])
        sc.RESULTS.clear()

    def test_script_is_executable_python_with_main_guard(self):
        src = Path(sc.__file__).read_text(encoding="utf-8")
        self.assertTrue(src.startswith("#!/usr/bin/env python3"))
        self.assertIn('if __name__ == "__main__":', src)

    def test_stdlib_only(self):
        import ast
        tree = ast.parse(Path(sc.__file__).read_text(encoding="utf-8"))
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        import sys
        self.assertTrue(imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
