import re
import time
from pathlib import Path
from typing import Any


class AggregateCardImageRenderer:
    """Render a cron aggregate digest into a local PNG image."""

    WIDTH = 960
    OUTER_PAD = 30
    CARD_PAD = 48
    HEADER_H = 118
    FOOTER_H = 70
    BODY_TOP_MARGIN = 28
    BODY_BOTTOM_MARGIN = 28
    SECTION_GAP = 22
    MAX_CACHE_FILES = 20

    def __init__(self, *, package_dir: str | Path | None = None) -> None:
        self._package_dir = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parent
        self._font_dir = self._package_dir / "assets" / "fonts"
        self._logo_path = self._package_dir / "logo.png"

    def render(self, digest: dict[str, Any], output_dir: str | Path) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps
        except ImportError as exc:  # pragma: no cover - depends on runtime image stack.
            raise RuntimeError("Pillow is required for aggregate image rendering") from exc

        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)

        title_font = self._load_font(ImageFont, 29)
        meta_font = self._load_font(ImageFont, 15)
        section_title_font = self._load_font(ImageFont, 20)
        body_font = self._load_font(ImageFont, 16)
        small_font = self._load_font(ImageFont, 14)

        title = str(digest.get("title", "") or "").strip() or "RSS 聚合"
        source_text = str(digest.get("source_text", "") or "").strip()
        item_count = int(digest.get("item_count", 0) or 0)
        meta = source_text or f"条目数：{item_count}"
        sections = self._normalize_sections(digest)

        measure_img = Image.new("RGB", (self.WIDTH, 10), "white")
        measure_draw = ImageDraw.Draw(measure_img)
        body_width = self.WIDTH - self.OUTER_PAD * 2 - self.CARD_PAD * 2
        section_layouts = []
        body_h = 0
        for section in sections:
            has_image = bool(section.get("image_path"))
            text_width = body_width - 170 if has_image else body_width
            title_lines = self._wrap_line(measure_draw, section["title"], section_title_font, text_width, max_lines=2)
            summary_lines = self._wrap_line(measure_draw, section["summary"], body_font, text_width, max_lines=4)
            title_h = len(title_lines) * (self._font_height(measure_draw, section_title_font) + 5)
            summary_h = len(summary_lines) * (self._font_height(measure_draw, body_font) + 6)
            section_h = max(104 if has_image else 0, title_h + summary_h + 10)
            section_layouts.append((section, title_lines, summary_lines, section_h))
            body_h += section_h
        if section_layouts:
            body_h += self.SECTION_GAP * (len(section_layouts) - 1)
        body_h = max(body_h, 82)

        total_h = (
            self.OUTER_PAD * 2 + self.HEADER_H + self.BODY_TOP_MARGIN + body_h
            + self.BODY_BOTTOM_MARGIN + self.FOOTER_H
        )
        img = Image.new("RGB", (self.WIDTH, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        self._draw_frame(draw, img.width, img.height)
        self._draw_header(draw, Image, img, title, meta, item_count, title_font, meta_font, small_font)
        self._draw_sections(draw, Image, ImageOps, img, section_layouts, section_title_font, body_font)
        self._draw_footer(draw, Image, img, small_font)

        path = output_root / f"{self._safe_id(digest)}_{int(time.time())}.png"
        img.save(path, format="PNG")
        self._trim_output_dir(output_root)
        return str(path)

    def _draw_frame(self, draw, width: int, height: int) -> None:
        x0 = self.OUTER_PAD
        y0 = self.OUTER_PAD
        x1 = width - self.OUTER_PAD
        y1 = height - self.OUTER_PAD
        draw.rounded_rectangle((x0, y0, x1, y1), radius=24, fill=(255, 255, 255), outline=(216, 222, 232), width=3)
        draw.rounded_rectangle((x0 + 8, y0 + 8, x1 - 8, y1 - 8), radius=18, outline=(240, 243, 248), width=1)

    def _draw_header(self, draw, Image, img, title: str, meta: str, item_count: int, title_font, meta_font, small_font) -> None:
        left = self.OUTER_PAD + self.CARD_PAD
        top = self.OUTER_PAD + 30
        logo = self._open_image(Image, self._logo_path)
        x = left
        if logo is not None:
            logo_img = self._fit_image(logo, 44, 44)
            self._paste_rgba(img, logo_img, (x, top + 2))
            x += 62
        right = self.WIDTH - self.OUTER_PAD - self.CARD_PAD
        badge = f"RSS AGGREGATE · {item_count} ITEMS"
        badge_w = self._text_width(draw, badge, small_font) + 28
        draw.rounded_rectangle((right - badge_w, top + 4, right, top + 38), radius=12, fill=(255, 126, 28))
        draw.text((right - badge_w + 14, top + 13), badge, font=small_font, fill=(255, 255, 255))
        text_right = right - badge_w - 28
        draw.text((x, top), self._ellipsize_text(draw, title, title_font, text_right - x), font=title_font, fill=(17, 24, 39))
        draw.text((x, top + 42), self._ellipsize_text(draw, meta, meta_font, right - x), font=meta_font, fill=(99, 110, 126))
        y = self.OUTER_PAD + self.HEADER_H
        draw.line((left, y, right, y), fill=(229, 233, 240), width=2)

    def _draw_sections(self, draw, Image, ImageOps, img, layouts, title_font, body_font) -> None:
        left = self.OUTER_PAD + self.CARD_PAD
        right = self.WIDTH - self.OUTER_PAD - self.CARD_PAD
        y = self.OUTER_PAD + self.HEADER_H + self.BODY_TOP_MARGIN
        for section, title_lines, summary_lines, section_h in layouts:
            x_text = left
            image_path = str(section.get("image_path", "") or "").strip()
            if image_path:
                thumb = self._open_image(Image, Path(image_path))
                if thumb is not None:
                    thumb = self._cover_image(thumb, 144, 96, ImageOps)
                    self._paste_rgba(img, thumb, (left, y + 4))
                    x_text = left + 170
            draw.text((x_text, y), "\n".join(title_lines), font=title_font, fill=(17, 24, 39), spacing=5)
            title_h = len(title_lines) * (self._font_height(draw, title_font) + 5)
            draw.text((x_text, y + title_h + 6), "\n".join(summary_lines), font=body_font, fill=(55, 65, 81), spacing=6)
            y += section_h + self.SECTION_GAP
        if not layouts:
            draw.text((left, y), "暂无聚合内容", font=body_font, fill=(55, 65, 81))
        draw.line((left, img.height - self.OUTER_PAD - self.FOOTER_H, right, img.height - self.OUTER_PAD - self.FOOTER_H), fill=(239, 242, 247), width=1)

    def _draw_footer(self, draw, Image, img, small_font) -> None:
        left = self.OUTER_PAD + self.CARD_PAD
        y = img.height - self.OUTER_PAD - self.FOOTER_H + 22
        text = "Powered by AstrBot · RSS Forwarder"
        draw.text((left, y), text, font=small_font, fill=(75, 85, 99))

    @staticmethod
    def _normalize_sections(digest: dict[str, Any]) -> list[dict[str, str]]:
        sections = digest.get("sections") or []
        normalized: list[dict[str, str]] = []
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                title = str(section.get("title", "") or "").strip()
                summary = str(section.get("summary", "") or "").strip()
                if not title and not summary:
                    continue
                normalized.append(
                    {
                        "title": title or summary,
                        "summary": summary or title,
                        "image_path": str(section.get("image_path", "") or "").strip(),
                    }
                )
        return normalized

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
    def _cover_image(img, width: int, height: int, ImageOps):
        from PIL import Image as PilImage

        resample = getattr(getattr(PilImage, "Resampling", object), "LANCZOS", 1)
        return ImageOps.fit(img, (width, height), method=resample, centering=(0.5, 0.5))

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
    def _wrap_line(cls, draw, text: str, font, max_width: int, *, max_lines: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in str(text or "").strip():
            candidate = current + char
            if current and cls._text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = char
                if len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and current:
            lines[-1] = cls._ellipsize_text(draw, lines[-1], font, max_width)
        return lines or [""]

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
    def _safe_id(digest: dict[str, Any]) -> str:
        raw = str(digest.get("id", "") or digest.get("job_id", "") or "aggregate")
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "aggregate"

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
