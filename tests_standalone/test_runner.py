"""scripts/run_tests.py parser, summariser and report writer (pure functions, no subprocess)."""
import unittest

from tests_standalone._common import load_module

rt = load_module("scripts/run_tests.py", "run_tests")

SAMPLE = """\
test_a (core.tests.test_x.Alpha.test_a) ... ok
test_b (core.tests.test_x.Alpha.test_b)
Docstring describing b. ... ok
test_c (core.tests.test_x.Beta.test_c) ... FAIL
test_d (core.tests.test_x.Beta.test_d) ... skipped 'no network'
login throttled ip=127.0.0.1
test_e (tests_standalone.test_y.Gamma.test_e) ... ERROR
test_f (core.tests.test_x.Beta.test_f) ... deploy-setup rejected ip=127.0.0.1
ok
"""


class Parser(unittest.TestCase):
    def setUp(self):
        self.results = rt.parse_unittest_output(SAMPLE)

    def test_counts_and_statuses(self):
        self.assertEqual([r[3] for r in self.results], ["ok", "ok", "FAIL", "skipped", "ERROR", "ok"])

    def test_module_and_class_split(self):
        self.assertEqual(self.results[0][:3], ("core.tests.test_x", "Alpha", "test_a"))
        self.assertEqual(self.results[4][:2], ("tests_standalone.test_y", "Gamma"))

    def test_docstring_style_result_on_next_line(self):
        self.assertEqual(self.results[1][2], "test_b")

    def test_result_on_its_own_line_after_log_noise(self):
        self.assertEqual(self.results[5][2:], ("test_f", "ok"))

    def test_log_noise_between_tests_is_ignored(self):
        self.assertEqual(len(self.results), 6)

    def test_summarize_and_totals(self):
        tree = rt.summarize(self.results)
        self.assertEqual(tree["core.tests.test_x"]["Beta"], {"FAIL": 1, "skipped": 1, "ok": 1})
        c = rt.totals(self.results)
        self.assertEqual((c["total"], c["ok"], c["FAIL"], c["ERROR"], c["skipped"]), (6, 3, 1, 1, 1))

    def test_verdict(self):
        self.assertEqual(rt.verdict(rt.totals(self.results)), "FAIL")
        self.assertEqual(rt.verdict(rt.totals(self.results[:2])), "PASS")


class Report(unittest.TestCase):
    META = {"when": "2026-01-01 00:00:00", "python": "3.13", "platform": "linux", "django": "5.2", "command": "x"}

    def suite(self, name, results, status="RAN"):
        return {"name": name, "status": status, "results": results, "seconds": 1.5, "output": "", "note": "why", "returncode": 0}

    def test_green_report_structure(self):
        ok = [("tests_standalone.test_frontend_static", "JavaScriptHygiene", "t", "ok")]
        text = rt.render_report(self.META, [self.suite("standalone", ok)])
        for needle in ("VerifySMS  -  TEST REPORT", "SUMMARY", "DEPENDENCY MODES", "SUITE: standalone",
                       "KNOWN OPEN ITEMS", "NOT COVERED BY AUTOMATED TESTS", "JavaScriptHygiene", "all passed"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"TOTAL\s+PASS")

    def test_skipped_suite_explains_why(self):
        text = rt.render_report(self.META, [self.suite("full", [], status="SKIPPED")])
        self.assertIn("SKIPPED - why", text)

    def test_failures_are_listed_by_name(self):
        bad = [("core.tests.test_x", "Beta", "test_c", "FAIL")]
        text = rt.render_report(self.META, [self.suite("full", bad)])
        self.assertIn("FAILURES", text)
        self.assertIn("core.tests.test_x.Beta.test_c", text)
        self.assertRegex(text, r"TOTAL\s+FAIL")


class Dependencies(unittest.TestCase):
    def test_missing_dependencies_returns_only_absent_modules(self):
        self.assertTrue(set(rt.missing_dependencies()) <= set(rt.REQUIRED_MODULES))

    def test_required_modules_match_requirements_txt(self):
        from tests_standalone._common import read
        reqs = read("requirements.txt").lower()
        self.assertIn("django", reqs)
        self.assertIn("dj-database-url", reqs)
        self.assertIn("psycopg", reqs)

    def test_every_documented_module_exists(self):
        from tests_standalone._common import ROOT
        for dotted in rt.MODULE_NOTES:
            self.assertTrue((ROOT / (dotted.replace(".", "/") + ".py")).exists(), dotted)


if __name__ == "__main__":
    unittest.main()
