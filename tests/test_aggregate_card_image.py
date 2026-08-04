import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("aggregate_card_image", ROOT / "aggregate_card_image.py")
MODULE = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

AggregateCardImageRenderer = MODULE.AggregateCardImageRenderer
MARKER_COLOR = (255, 0, 255)
BODY_TITLE_COLOR = (17, 24, 39)
BODY_SUMMARY_COLOR = (55, 65, 81)


class AggregateCardImageRendererTests(unittest.TestCase):
    def test_short_card_keeps_last_thumbnail_out_of_footer(self):
        sections = [{"title": "Short title", "summary": "Short summary"}]

        self._assert_last_thumbnail_stays_above_footer(sections)

    def test_long_card_keeps_last_thumbnail_out_of_footer(self):
        sections = [
            {
                "title": f"Section {index}",
                "summary": "Short summary",
            }
            for index in range(1, 9)
        ]

        self._assert_last_thumbnail_stays_above_footer(sections)

    def test_last_text_only_section_keeps_long_content_out_of_footer(self):
        renderer = AggregateCardImageRenderer(package_dir=ROOT)
        long_title = "Long text-only section title " * 12
        long_summary = "Long text-only section summary " * 30
        self._assert_wrapped_line_counts(renderer, long_title, long_summary)
        digest = {
            "id": "text-only-footer-spacing",
            "title": "Aggregate card",
            "source_text": "Example Feed",
            "item_count": 2,
            "sections": [
                {"title": "Earlier section", "summary": "Earlier summary"},
                {"title": long_title, "summary": long_summary},
            ],
        }

        with TemporaryDirectory() as tmpdir:
            output_path = renderer.render(digest, tmpdir)

            with Image.open(output_path) as image:
                card = image.convert("RGB")
                body, footer = self._crop_body_and_footer(card, renderer)
                body_colors = body.getcolors(maxcolors=body.width * body.height)
                footer_colors = footer.getcolors(maxcolors=footer.width * footer.height)
                body_color_set = {color for _count, color in body_colors}
                footer_color_set = {color for _count, color in footer_colors}

                self.assertIn(BODY_TITLE_COLOR, body_color_set)
                self.assertIn(BODY_SUMMARY_COLOR, body_color_set)
                self.assertNotIn(BODY_TITLE_COLOR, footer_color_set)
                self.assertNotIn(BODY_SUMMARY_COLOR, footer_color_set)

    def _assert_last_thumbnail_stays_above_footer(self, sections):
        renderer = AggregateCardImageRenderer(package_dir=ROOT)
        with TemporaryDirectory() as tmpdir:
            marker_path = Path(tmpdir) / "last-thumbnail.png"
            Image.new("RGB", (144, 96), MARKER_COLOR).save(marker_path)
            sections[-1]["image_path"] = str(marker_path)
            digest = {
                "id": "footer-spacing",
                "title": "Aggregate card",
                "source_text": "Example Feed",
                "item_count": len(sections),
                "sections": sections,
            }

            output_path = renderer.render(digest, tmpdir)

            with Image.open(output_path) as image:
                card = image.convert("RGB")
                body, footer = self._crop_body_and_footer(card, renderer)
                body_colors = body.getcolors(maxcolors=body.width * body.height)
                footer_colors = footer.getcolors(maxcolors=footer.width * footer.height)

                self.assertIn(
                    MARKER_COLOR,
                    {color for _count, color in body_colors},
                    "last thumbnail is missing from the body",
                )
                self.assertNotIn(
                    MARKER_COLOR,
                    {color for _count, color in footer_colors},
                    "last thumbnail overlaps the footer reservation",
                )

    def _assert_wrapped_line_counts(self, renderer, title, summary):
        measure_image = Image.new("RGB", (renderer.WIDTH, 10), "white")
        measure_draw = ImageDraw.Draw(measure_image)
        body_width = renderer.WIDTH - renderer.OUTER_PAD * 2 - renderer.CARD_PAD * 2
        title_font = renderer._load_font(ImageFont, 20)
        summary_font = renderer._load_font(ImageFont, 16)

        title_lines = renderer._wrap_line(measure_draw, title, title_font, body_width, max_lines=2)
        summary_lines = renderer._wrap_line(measure_draw, summary, summary_font, body_width, max_lines=4)

        self.assertEqual(2, len(title_lines))
        self.assertEqual(4, len(summary_lines))

    @staticmethod
    def _crop_body_and_footer(card, renderer):
        body_top = renderer.OUTER_PAD + renderer.HEADER_H
        footer_top = card.height - renderer.OUTER_PAD - renderer.FOOTER_H
        body = card.crop((0, body_top, card.width, footer_top))
        footer = card.crop((0, footer_top, card.width, card.height))
        return body, footer


if __name__ == "__main__":
    unittest.main()
