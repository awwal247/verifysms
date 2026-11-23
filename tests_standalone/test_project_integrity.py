"""Repository integrity - runs on a bare Python install (no pip install needed)."""
import ast
import re
import unittest

from tests_standalone._common import ROOT, env_names, project_files, read

VIEWS = read("core/views.py")


class SourceFiles(unittest.TestCase):
    def test_every_python_file_parses(self):
        for path in project_files(".py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_every_python_file_is_valid_utf8_without_tabs_indentation_errors(self):
        for path in project_files(".py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_no_debug_leftovers(self):
        for path in project_files(".py"):
            if "tests" in path.parts or "tests_standalone" in path.parts or path.parts[-2] == "scripts":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("breakpoint()", text, path.name)
            self.assertNotIn("import pdb", text, path.name)

    def test_no_hardcoded_secrets_in_tracked_text_files(self):
        patterns = [r"sk_live_[A-Za-z0-9]{10,}", r"sk_test_[A-Za-z0-9]{20,}", r"pk_live_[A-Za-z0-9]{10,}",
                    r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", r"postgres(ql)?://[^\s:@/]+:[^\s@]{4,}@(?!host\b|HOST\b)[\w.-]+"]
        for path in project_files(".py", ".js", ".html", ".md", ".txt", ".sql", ".example", ".json", ".css"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pat in patterns:
                m = re.search(pat, text)
                # README documents the URL shape with placeholder PASSWORD / xxxx
                if m and "PASSWORD" not in m.group(0) and "xxxx" not in m.group(0):
                    self.fail(f"possible secret in {path.relative_to(ROOT)}: {m.group(0)[:30]}...")

    def test_settings_do_not_embed_production_secret(self):
        settings = read("verify_sms/settings.py")
        self.assertIn('os.getenv("DJANGO_SECRET_KEY", "")', settings)
        self.assertIn('DEBUG = _flag("DJANGO_DEBUG", "0")', settings)   # secure by default


class RepoHygiene(unittest.TestCase):
    def test_gitignore_protects_secrets_and_local_state(self):
        lines = {l.strip() for l in read(".gitignore").splitlines()}
        for needed in (".env*", "!.env.example", "db.sqlite3", "__pycache__/", ".venv/", ".vercel"):
            self.assertIn(needed, lines)

    def test_env_example_has_no_real_values(self):
        for line in read(".env.example").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                if key.endswith(("KEY", "TOKEN", "SECRET", "PASSWORD")) or key in {"DATABASE_URL", "REDIS_URL"}:
                    self.assertEqual(value.strip(), "", f"{key} must be blank in .env.example")

    def test_every_env_var_the_code_reads_is_documented(self):
        sources = [p.read_text(encoding="utf-8") for p in project_files(".py")
                   if "tests" not in p.parts and "tests_standalone" not in p.parts]
        used = env_names(*sources)
        documented = read(".env.example") + read("README.md")
        undocumented = sorted(v for v in used if v not in documented)
        self.assertEqual(undocumented, [], "add these to .env.example or README.md")

    def test_requirements_are_bounded(self):
        lines = [l.strip() for l in read("requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"[<>=~]=?\s*[\d.]+.*,\s*<\s*[\d.]+|==", f"{line!r} needs an upper bound or pin")
        names = {re.split(r"[<>=\[~]", l)[0].lower() for l in lines}
        self.assertTrue({"django", "dj-database-url", "psycopg"} <= names)

    def test_vercel_files_do_not_contain_tokens(self):
        for path in (ROOT / ".vercel").glob("*"):
            self.assertNotRegex(path.read_text(encoding="utf-8", errors="ignore"), r"(?i)token\s*[:=]\s*\S{12,}", path.name)

    def test_env_local_is_not_meant_to_be_committed(self):
        # .env.local may exist on a developer machine; .gitignore (tested above) keeps it out of git.
        self.assertIn(".env*", read(".gitignore"))


class Migrations(unittest.TestCase):
    def _files(self):
        return sorted(p for p in (ROOT / "core/migrations").glob("0*.py"))

    def test_numbering_is_contiguous(self):
        nums = [int(p.name[:4]) for p in self._files()]
        self.assertEqual(nums, list(range(1, len(nums) + 1)))

    def test_each_migration_depends_on_its_predecessor(self):
        files = self._files()
        for prev, cur in zip(files, files[1:]):
            text = cur.read_text(encoding="utf-8")
            self.assertRegex(text, r"""\(\s*["']core["']\s*,\s*["']%s["']\s*\)""" % re.escape(prev.stem), cur.name)

    def test_initial_migration_marked_initial(self):
        self.assertRegex(self._files()[0].read_text(encoding="utf-8"), r"initial\s*=\s*True")


class RoutingAndViews(unittest.TestCase):
    URLS = read("core/urls.py")

    def test_every_routed_view_exists(self):
        defined = set(re.findall(r"^def (\w+)\(", VIEWS, flags=re.M)) | \
                  set(re.findall(r"^def (\w+)\(", read("core/well_known.py"), flags=re.M))
        routed = set(re.findall(r"(?:views|well_known)\.(\w+),\s*name=", self.URLS))
        self.assertEqual(sorted(routed - defined), [])

    def test_route_names_are_unique(self):
        names = re.findall(r'name="([^"]+)"', self.URLS)
        self.assertEqual(len(names), len(set(names)))

    def test_state_changing_endpoints_restrict_http_methods(self):
        for fn in ("orders_api", "paystack_init", "paystack_verify", "paystack_webhook", "admin_action", "provider_api"):
            m = re.search(r"((?:@\w[^\n]*\n)+)def %s\(" % fn, VIEWS)
            self.assertIsNotNone(m, fn)
            self.assertIn("require_", m.group(1), f"{fn} lacks a method restriction")

    def test_webhook_is_csrf_exempt_but_signature_checked(self):
        self.assertIn("validate_webhook", VIEWS)
        m = re.search(r"((?:@\w[^\n]*\n)+)def paystack_webhook\(", VIEWS)
        self.assertIn("csrf_exempt", m.group(1))

    def test_views_render_only_existing_templates(self):
        for name in set(re.findall(r'render\(request, "([^"{}]+\.html)"', VIEWS)):
            self.assertTrue((ROOT / "templates" / name).exists(), name)


class ProjectDocs(unittest.TestCase):
    def test_readme_mentions_both_test_modes(self):
        readme = read("README.md")
        for needle in ("scripts/run_tests.py", "--standalone", "--full"):
            self.assertIn(needle, readme)

    def test_docs_exist(self):
        for rel in ("README.md", "docs/SECURITY_HARDENING.md", "docs/TESTING.md"):
            self.assertTrue((ROOT / rel).exists(), rel)


if __name__ == "__main__":
    unittest.main()
