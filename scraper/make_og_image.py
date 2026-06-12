"""Generate site/og-image.png — the 1200x630 card WhatsApp/Facebook/X show
when someone shares a link to the site.

Run once locally whenever the design changes (`py scraper/make_og_image.py`).
Needs Pillow (`pip install pillow`), which is deliberately NOT in
requirements.txt: the daily GitHub Actions workflow never runs this.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SITE_DIR = Path(__file__).resolve().parents[1] / "site"
W, H = 1200, 630
GREEN_DARK = (20, 89, 47)    # --green-dark
GREEN = (29, 122, 70)        # --green
FONTS = Path("C:/Windows/Fonts")


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def main():
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    # Vertical gradient matching the site header.
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(
            round(GREEN_DARK[i] + (GREEN[i] - GREEN_DARK[i]) * t) for i in range(3)))

    # A price-history line, the site's signature visual.
    pts = [(80, 470), (215, 430), (350, 455), (485, 380), (620, 415),
           (755, 330), (890, 370), (1025, 290), (1120, 310)]
    draw.line(pts, fill=(255, 255, 255, 90), width=7, joint="curve")
    for p in pts:
        draw.ellipse([p[0] - 11, p[1] - 11, p[0] + 11, p[1] + 11],
                     fill=(255, 255, 255))
        draw.ellipse([p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5], fill=GREEN_DARK)

    draw.text((80, 110), "T&T Produce Prices",
              font=font("segoeuib.ttf", 86), fill=(255, 255, 255))
    draw.text((82, 225), "Daily wholesale prices from the Macoya market",
              font=font("segoeui.ttf", 40), fill=(223, 236, 226))
    draw.text((82, 555), "ttproduceprices.com",
              font=font("segoeuib.ttf", 32), fill=(195, 219, 201))

    out = SITE_DIR / "og-image.png"
    img.save(out, optimize=True)
    print(f"{out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
