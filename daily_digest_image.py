import re
import time
from pathlib import Path
from typing import Any


class DailyDigestImageRenderer:
    """Render daily digest content into a local PNG image."""

    WIDTH = 960
    OUTER_PAD = 30
    CARD_PAD = 54
    HEADER_H = 132
    FOOTER_H = 84
    BODY_TOP_MARGIN = 36
    BODY_BOTTOM_MARGIN = 46
    LINE_GAP = 8
    MAX_CACHE_FILES = 16

    def __init__(
        self,
        *,
        asset_dir: str | Path | None = None,
        package_dir: str | Path | None = None,
    ) -> None:
        self._package_dir = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parent
        self._asset_dir = Path(asset_dir) if asset_dir is not None else self._package_dir / "assets" / "daily_digest"
        self._font_dir = self._package_dir / "assets" / "fonts"
        self._logo_path = self._package_dir / "logo.png"

    def render(self, digest: dict[str, Any], output_dir: str | Path) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:  # pragma: no cover - depends on runtime image stack.
            raise RuntimeError("Pillow is required for daily digest image rendering") from exc

        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)

        title_font = self._load_font(ImageFont, 28)
        meta_font = self._load_font(ImageFont, 15)
        body_font = self._load_font(ImageFont, 18)
        small_font = self._load_font(ImageFont, 14)

        title = str(digest.get("title", "") or "").strip() or "RSS 日报"
        window_start = str(digest.get("window_start_text", "") or "").strip()
        window_end = str(digest.get("window_end_text", "") or "").strip()
        item_count = int(digest.get("item_count", 0) or 0)
        content = str(digest.get("content", "") or "").strip()
        date_label = self._date_label(window_end)
        window_text = f"{window_start} - {window_end}" if window_start and window_end else ""
        meta_text = f"统计区间：{window_text} · 条目数：{item_count}" if window_text else f"条目数：{item_count}"

        measure_img = Image.new("RGB", (self.WIDTH, 10), "white")
        measure_draw = ImageDraw.Draw(measure_img)
        body_width = self.WIDTH - self.OUTER_PAD * 2 - self.CARD_PAD * 2
        body_lines = self._wrap_multiline_text(measure_draw, content, body_font, body_width)
        line_h = self._font_height(measure_draw, body_font) + self.LINE_GAP
        body_h = max(line_h, len(body_lines) * line_h)
        total_h = (
            self.OUTER_PAD * 2
            + self.HEADER_H
            + self.BODY_TOP_MARGIN
            + body_h
            + self.BODY_BOTTOM_MARGIN
            + self.FOOTER_H
        )

        img = Image.new("RGB", (self.WIDTH, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        self._draw_frame(draw, Image, img, img.width, img.height)
        self._draw_header(draw, Image, img, title, meta_text, date_label, title_font, meta_font, small_font)
        self._draw_body(draw, body_lines, body_font)
        self._draw_footer(Image, img, draw, small_font)

        path = output_root / f"{self._safe_id(digest)}_{int(time.time())}.png"
        img.save(path, format="PNG")
        self._trim_output_dir(output_root)
        return str(path)

    def _draw_frame(self, draw, Image, img, width: int, height: int) -> None:
        x0 = self.OUTER_PAD
        y0 = self.OUTER_PAD
        x1 = width - self.OUTER_PAD
        y1 = height - self.OUTER_PAD
        draw.rounded_rectangle((x0, y0, x1, y1), radius=28, fill=(255, 255, 255), outline=(216, 222, 232), width=3)
        draw.rounded_rectangle((x0 + 8, y0 + 8, x1 - 8, y1 - 8), radius=22, outline=(239, 242, 247), width=1)

        accent = self._open_asset(Image, "corner_accent.png")
        if accent is not None:
            small = self._fit_image(accent, 102, 102)
            self._paste_rgba(img, small, (x1 - small.width - 22, y1 - small.height - 22))
        else:
            draw.line((x1 - 96, y1 - 24, x1 - 24, y1 - 96), fill=(255, 126, 28), width=5)

    def _draw_header(self, draw, Image, img, title: str, meta_text: str, date_label: str, title_font, meta_font, small_font) -> None:
        left = self.OUTER_PAD + self.CARD_PAD
        top = self.OUTER_PAD + 30
        rss_mark = self._open_asset(Image, "rss_mark.png")

        x = left
        if rss_mark is not None:
            mark = self._fit_image(rss_mark, 38, 38)
            self._paste_rgba(img, mark, (x, top + 6))
            x += 56

        right = self.WIDTH - self.OUTER_PAD - self.CARD_PAD
        badge = self._open_asset(Image, "header_badge.png")
        badge_width = 0
        if badge is not None:
            badge_img = self._fit_image(badge, 198, 40)
            badge_width = badge_img.width
            self._paste_rgba(img, badge_img, (right - badge_img.width, top + 6))
        text_right = right - badge_width - 30 if badge_width else right
        header_text_width = max(1, text_right - x)
        title = self._ellipsize_text(draw, title, title_font, header_text_width)
        meta_text = self._ellipsize_text(draw, meta_text, meta_font, header_text_width)
        draw.text((x, top), title, font=title_font, fill=(17, 24, 39))
        draw.text((x, top + 40), meta_text, font=meta_font, fill=(99, 110, 126))
        draw.text(
            (right - self._text_width(draw, date_label, small_font), top + 55),
            date_label,
            font=small_font,
            fill=(255, 126, 28),
        )
        y = self.OUTER_PAD + self.HEADER_H
        draw.line((left, y, self.WIDTH - self.OUTER_PAD - self.CARD_PAD, y), fill=(229, 233, 240), width=2)

    def _draw_body(self, draw, lines: list[str], font) -> None:
        x = self.OUTER_PAD + self.CARD_PAD
        y = self.OUTER_PAD + self.HEADER_H + self.BODY_TOP_MARGIN
        line_h = self._font_height(draw, font) + self.LINE_GAP
        for line in lines:
            draw.text((x, y), line, font=font, fill=(31, 41, 55))
            y += line_h

    def _draw_footer(self, Image, img, draw, small_font) -> None:
        y = img.height - self.OUTER_PAD - self.FOOTER_H + 16
        x = self.OUTER_PAD + self.CARD_PAD
        plugin_logo = self._open_image(Image, self._logo_path)
        if plugin_logo is not None:
            logo = self._fit_image(plugin_logo, 34, 34)
            self._paste_rgba(img, logo, (x, y + 2))
            x += 48
        text = "Powered by AstrBot · RSS Forwarder"
        draw.text((x, y + 10), text, font=small_font, fill=(75, 85, 99))

    def _open_asset(self, Image, name: str):
        return self._open_image(Image, self._asset_dir / name)

    @staticmethod
    def _open_image(Image, path: Path):
        try:
            if path.exists():
                return Image.open(path).convert("RGBA")
        except Exception:
            return None
        return None

    @staticmethod
    def _fit_image(img, max_w: int, max_h: int):
        if img.width <= max_w and img.height <= max_h:
            return img
        ratio = min(max_w / img.width, max_h / img.height)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        from PIL import Image as PilImage

        resample = getattr(getattr(PilImage, "Resampling", object), "LANCZOS", 1)
        return img.resize(new_size, resample)

    @staticmethod
    def _paste_rgba(base, overlay, pos: tuple[int, int]) -> None:
        base.paste(overlay, pos, overlay if overlay.mode == "RGBA" else None)

    def _load_font(self, ImageFont, size: int):
        for path in self._font_candidates():
            try:
                if path.exists():
                    return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _font_candidates(self) -> list[Path]:
        return [
            self._font_dir / "wqy-zenhei.ttc",
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/mnt/c/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyh.ttc"),
        ]

    @classmethod
    def _wrap_multiline_text(cls, draw, text: str, font, max_width: int) -> list[str]:
        if not text:
            return ["暂无日报内容"]
        lines: list[str] = []
        for paragraph in text.splitlines():
            if not paragraph.strip():
                lines.append("")
                continue
            lines.extend(cls._wrap_line(draw, paragraph.strip(), font, max_width))
        return lines or ["暂无日报内容"]

    @classmethod
    def _wrap_line(cls, draw, text: str, font, max_width: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if current and cls._text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _text_width(draw, text: str, font) -> int:
        if not text:
            return 0
        box = draw.textbbox((0, 0), text, font=font)
        return int(box[2] - box[0])

    @classmethod
    def _ellipsize_text(cls, draw, text: str, font, max_width: int) -> str:
        if cls._text_width(draw, text, font) <= max_width:
            return text
        suffix = "..."
        suffix_width = cls._text_width(draw, suffix, font)
        current = ""
        for char in text:
            candidate = current + char
            if cls._text_width(draw, candidate, font) + suffix_width > max_width:
                return current + suffix if current else suffix
            current = candidate
        return current

    @staticmethod
    def _font_height(draw, font) -> int:
        box = draw.textbbox((0, 0), "测Ag", font=font)
        return int(box[3] - box[1])

    @staticmethod
    def _date_label(window_end: str) -> str:
        text = window_end.strip()
        if not text:
            return time.strftime("%Y-%m-%d")
        return text.split()[0].replace("/", "-")

    @staticmethod
    def _safe_id(digest: dict[str, Any]) -> str:
        raw = str(digest.get("id", "") or "daily_digest")
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "daily_digest"

    @classmethod
    def _trim_output_dir(cls, output_dir: Path) -> None:
        try:
            files = sorted(
                [path for path in output_dir.iterdir() if path.is_file() and path.suffix.lower() == ".png"],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in files[cls.MAX_CACHE_FILES :]:
                path.unlink(missing_ok=True)
        except Exception:
            return
