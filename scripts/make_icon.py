"""Generate the application icon with Pillow (assets/qslcard.ico)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NAVY = (11, 61, 145, 255)
WHITE = (255, 255, 255, 255)
PALE = (205, 220, 245, 255)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def build(destination: str | Path = "assets/qslcard.ico") -> Path:
    size = 256
    image = Image.new("RGBA", (size, size), NAVY)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([12, 12, size - 12, size - 12], radius=36, outline=WHITE, width=10)
    draw.text((size / 2, size / 2 - 12), "QSL", font=_font(86), fill=WHITE, anchor="mm")
    draw.text((size / 2, size - 64), "CARD", font=_font(26), fill=PALE, anchor="mm")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
    return target


if __name__ == "__main__":
    print(build())
