"""Static checks on assets/ and templates/ - no Django, no browser, no network."""
import re
import unittest

from tests_standalone._common import ROOT, project_files, read


class JavaScriptHygiene(unittest.TestCase):
    JS = read("assets/js/app.js")

    def test_no_eval_or_function_constructor(self):
        self.assertIsNone(re.search(r"\beval\s*\(", self.JS))
        self.assertIsNone(re.search(r"new\s+Function\s*\(", self.JS))

    def test_no_document_write(self):
        self.assertNotIn("document.write(", self.JS)

    def test_provider_and_sms_data_never_interpolated_into_html(self):
        # Regression for the stored-XSS fixes (SMS text / provider messages).
        self.assertNotIn("${message}", self.JS)
        self.assertNotIn("${data.sms_code}", self.JS)
        self.assertNotIn("' + data.sms_text + '", self.JS)

    def test_delegated_copy_handler_present(self):
        self.assertIn("data-copy", self.JS)

    def test_csrf_header_is_sent(self):
        self.assertIn("X-CSRFToken", self.JS)

    def test_no_hardcoded_secrets_in_js(self):
        self.assertIsNone(re.search(r"(sk_live|sk_test|pk_live)_[A-Za-z0-9]{8,}", self.JS))


class TemplateHygiene(unittest.TestCase):
    def test_no_jinja_value_inside_inline_js_string(self):
        # copyText('{{ ... }}') let an apostrophe in SMS data break out of the JS string.
        for tpl in project_files(".html"):
            self.assertNotIn("copyText('{{", tpl.read_text(encoding="utf-8"), tpl.name)
        self.assertNotIn("copyText(\\'' + esc", read("templates/user/sms.html"))

    def test_no_autoescape_off_blocks(self):
        for tpl in project_files(".html"):
            self.assertNotIn("autoescape off", tpl.read_text(encoding="utf-8"), tpl.name)

    def test_safe_filter_is_not_used_on_user_or_provider_data(self):
        risky = re.compile(r"\{\{[^}]*(sms_|phone|email|first_name|message|description)[^}]*\|\s*safe\s*\}\}")
        for tpl in project_files(".html"):
            self.assertIsNone(risky.search(tpl.read_text(encoding="utf-8")), tpl.name)

    def test_forms_that_post_carry_csrf_token(self):
        for tpl in project_files(".html"):
            text = tpl.read_text(encoding="utf-8")
            for form in re.findall(r"<form\b[^>]*>.*?</form>", text, flags=re.S | re.I):
                if re.search(r"method=[\"']?post", form, flags=re.I):
                    self.assertIn("csrf_token", form, f"{tpl.name}: POST form without csrf_token")

    def test_external_scripts_are_https_and_on_known_hosts(self):
        allowed = ("https://cdnjs.cloudflare.com", "https://js.paystack.co")
        for tpl in project_files(".html"):
            for src in re.findall(r"<script[^>]+src=[\"'](https?:[^\"']+)", tpl.read_text(encoding="utf-8"), flags=re.I):
                self.assertTrue(src.startswith(allowed), f"{tpl.name}: {src}")

    def test_target_blank_links_have_noopener(self):
        for tpl in project_files(".html"):
            for tag in re.findall(r"<a\b[^>]*target=[\"']_blank[\"'][^>]*>", tpl.read_text(encoding="utf-8"), flags=re.I):
                self.assertIn("noopener", tag, f"{tpl.name}: {tag[:80]}")


# Assets referenced by templates but intentionally absent from the repo. Empty now that the tutorial
# videos are committed; add an entry here only for a known gap (a test fails when an entry goes stale).
KNOWN_MISSING_MEDIA = set()


def _missing_assets():
    missing = set()
    for tpl in project_files(".html"):
        for ref in re.findall(r"""(?:src|href|poster)=["'](?:\.\./|/)?assets/([^"'?#{}]+)""", tpl.read_text(encoding="utf-8")):
            if not (ROOT / "assets" / ref).exists():
                missing.add(ref)
    return missing


class AssetsAndDocs(unittest.TestCase):
    def test_referenced_local_assets_exist(self):
        self.assertEqual(_missing_assets() - KNOWN_MISSING_MEDIA, set())

    def test_known_missing_media_list_is_current(self):
        """Fails when a listed file has been added, so the allow-list cannot go stale."""
        self.assertEqual(KNOWN_MISSING_MEDIA - _missing_assets(), set(),
                         "these files now exist - remove them from KNOWN_MISSING_MEDIA")

    def test_core_assets_present_and_non_empty(self):
        for rel in ("assets/css/style.css", "assets/js/app.js", "assets/img/favicon.svg"):
            self.assertGreater((ROOT / rel).stat().st_size, 0, rel)

    def test_every_page_route_has_a_template(self):
        for page in ("dashboard", "services", "cart", "topup", "history", "settings", "sms", "tutorials", "notifications"):
            self.assertTrue((ROOT / f"templates/user/{page}.html").exists(), page)
        for page in ("dashboard", "users", "transactions", "providers", "payment", "coupons", "settings", "broadcast"):
            self.assertTrue((ROOT / f"templates/admin/{page}.html").exists(), page)
        for name in ("index", "login", "register", "logout", "maintenance", "404"):
            self.assertTrue((ROOT / f"templates/{name}.html").exists(), name)


if __name__ == "__main__":
    unittest.main()
