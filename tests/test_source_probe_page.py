from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = ROOT / "pages" / "source-diagnostics"
INDEX_PATH = PAGE_DIR / "index.html"
SCRIPT_PATH = PAGE_DIR / "app.js"
STYLE_PATH = PAGE_DIR / "style.css"
LOCALE_DIR = ROOT / ".astrbot-plugin" / "i18n"
LOCALES = ("zh-CN", "en-US", "ja-JP")
MODE_NAMES = (
    "direct_strict",
    "proxy_strict",
    "direct_relaxed",
    "proxy_relaxed",
)
REQUIRED_TRANSLATION_PATHS = (
    "title",
    "description",
    "headings.source",
    "headings.draft",
    "headings.results",
    "headings.recommendation",
    "source.label",
    "source.draft",
    "source.savedSummary",
    "sourceTypes.rss",
    "sourceTypes.twitter",
    "fields.url",
    "fields.nitterUrl",
    "fields.username",
    "fields.proxyUrl",
    "fields.timeout",
    "fields.verifySsl",
    "fields.authMode",
    "fields.temporaryKey",
    "fields.fullCheck",
    "auth.none",
    "auth.query",
    "auth.header",
    "actions.run",
    "actions.running",
    "results.status",
    "results.latency",
    "results.httpStatus",
    "results.feedRecognition",
    "results.error",
    "statuses.success",
    "statuses.failure",
    "statuses.skipped",
    "statuses.pending",
    "errors.load",
    "errors.request",
    "errors.validation",
    "recommendation.none",
    "recommendation.onlyThisSource",
    "recommendation.securityWarning",
)


def _read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"missing required file: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def _nested_value(values: dict[str, object], dotted_path: str) -> object:
    current: object = values
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise AssertionError(f"missing translation key: {dotted_path}")
        current = current[segment]
    return current


class _FormContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.labels_for: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if tag in {"input", "select"} and element_id:
            self.inputs[element_id] = attributes
        if tag == "label" and attributes.get("for"):
            self.labels_for.add(str(attributes["for"]))


class SourceProbePageContractTests(unittest.TestCase):
    def test_page_files_exist_and_use_relative_assets(self):
        for path in (INDEX_PATH, SCRIPT_PATH, STYLE_PATH):
            self.assertTrue(path.is_file(), f"missing {path.relative_to(ROOT)}")

        html = _read(INDEX_PATH)
        self.assertRegex(html, r'href=["\']\./style\.css["\']')
        self.assertRegex(html, r'src=["\']\./app\.js["\']')
        self.assertRegex(html, r'<script[^>]+type=["\']module["\']')

    def test_bridge_is_the_only_transport_and_uses_relative_endpoints(self):
        script = _read(SCRIPT_PATH)
        self.assertIn("window.AstrBotPluginPage", script)
        self.assertRegex(script, r"await\s+bridge\.ready\(\)")
        self.assertIn('bridge.apiGet("source-probe/feeds")', script)
        self.assertRegex(
            script,
            r'bridge\.apiPost\(\s*["\']source-probe/run["\']\s*,',
        )

        lowered = script.lower()
        forbidden = (
            "fetch(",
            "localstorage",
            "document.cookie",
            "window.parent",
            "/api/plugin/",
            "/api/v1/",
            "asset_token",
        )
        for marker in forbidden:
            self.assertNotIn(marker, lowered)

    def test_form_controls_are_labeled_and_secret_is_ephemeral(self):
        html = _read(INDEX_PATH)
        parser = _FormContractParser()
        parser.feed(html)

        expected_controls = {
            "source-select",
            "source-url",
            "nitter-url",
            "twitter-username",
            "proxy-url",
            "timeout",
            "verify-ssl",
            "auth-mode",
            "temporary-key",
            "full-check",
        }
        self.assertTrue(expected_controls.issubset(parser.inputs))
        self.assertTrue(expected_controls.issubset(parser.labels_for))

        secret = parser.inputs["temporary-key"]
        self.assertEqual(secret.get("type"), "password")
        self.assertEqual(secret.get("autocomplete"), "off")

        self.assertRegex(html, r'aria-live=["\']polite["\']')

    def test_request_lifecycle_disables_button_and_clears_key_in_finally(self):
        script = _read(SCRIPT_PATH)
        self.assertIn("runButton.disabled = true", script)
        self.assertRegex(
            script,
            r"finally\s*\{[\s\S]*?temporaryKeyInput\.value\s*=\s*[\"']{2}",
        )
        self.assertRegex(
            script,
            r"finally\s*\{[\s\S]*?runButton\.disabled\s*=\s*false",
        )

    def test_saved_and_draft_payloads_match_the_api_contract(self):
        script = _read(SCRIPT_PATH)
        self.assertRegex(
            script,
            r"return\s*\{\s*feed_id:\s*selectedSourceId,\s*full_check:\s*fullCheck\s*\}",
        )
        for field in (
            "source_type",
            "proxy_url",
            "timeout",
            "verify_ssl",
            "url",
            "auth_mode",
            "key",
            "username",
            "nitter_url",
        ):
            self.assertIn(f"{field}:", script)
        self.assertIn("timeout: Number(timeoutInput.value)", script)
        self.assertIn("verify_ssl: verifySslInput.checked", script)
        self.assertIn("full_check: fullCheckInput.checked", script)
        self.assertRegex(script, r"draft:\s*buildDraft\(\)")

    def test_source_type_visibility_fixed_modes_and_skipped_state_are_defined(self):
        script = _read(SCRIPT_PATH)
        self.assertIn("renderSourceFields", script)
        self.assertIn('sourceType === "rss"', script)
        self.assertIn('sourceType === "twitter"', script)
        for mode in MODE_NAMES:
            self.assertIn(f'"{mode}"', script)
        self.assertIn('status: "skipped"', script)

    def test_context_changes_refresh_translations_and_theme(self):
        script = _read(SCRIPT_PATH)
        self.assertIn("function renderTranslations", script)
        self.assertIn("bridge.onContext(renderTranslations)", script)
        self.assertIn("bridge.t(", script)
        self.assertIn("document.title", script)

    def test_errors_use_text_content_and_relaxed_tls_success_has_warning(self):
        script = _read(SCRIPT_PATH)
        self.assertRegex(
            script,
            r"errorText\.textContent\s*=\s*error\.message",
        )
        self.assertIn("recommendation.verify_ssl === false", script)
        self.assertIn("recommendation.securityWarning", script)
        self.assertIn("recommendation.onlyThisSource", script)

    def test_locale_files_parse_and_contain_complete_page_copy(self):
        for locale in LOCALES:
            path = LOCALE_DIR / f"{locale}.json"
            values = json.loads(_read(path))
            page = _nested_value(values, "pages.source-diagnostics")
            self.assertIsInstance(page, dict)
            for dotted_path in REQUIRED_TRANSLATION_PATHS:
                value = _nested_value(page, dotted_path)
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip(), f"empty {locale}: {dotted_path}")
            for mode in MODE_NAMES:
                value = _nested_value(page, f"modes.{mode}")
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip(), f"empty {locale}: modes.{mode}")

    def test_css_supports_themes_mobile_rows_and_operational_layout(self):
        style = _read(STYLE_PATH)
        self.assertRegex(style, r":root\s*\{[\s\S]*?--color-background:")
        self.assertRegex(
            style,
            r'\[data-theme=["\']dark["\']\]\s*\{[\s\S]*?--color-background:',
        )
        self.assertRegex(style, r"@media\s*\(max-width:\s*720px\)")
        self.assertIn("content: attr(data-label)", style)
        self.assertIn("overflow-wrap: anywhere", style)
        self.assertRegex(style, r"--control-height:\s*[0-9.]+rem")
        self.assertNotIn("linear-gradient", style)
        self.assertNotIn("radial-gradient", style)


if __name__ == "__main__":
    unittest.main()
