import os, glob
from PIL import Image, ImageChops, ImageDraw

SRC = os.path.join(os.path.dirname(__file__), "article2_imgs")
RADIUS = 22
BORDER = 2
BORDER_COLOR = (225, 228, 235, 255)
PAD = 6  # keep a little whitespace after autocrop


def autocrop_white(im, tol=12):
    rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg)
    # amplify small differences so near-white is treated as background
    diff = ImageChops.add(diff, diff, 2.0, -tol)
    bbox = diff.getbbox()
    if bbox:
        l, t, r, b = bbox
        l = max(0, l - PAD); t = max(0, t - PAD)
        r = min(im.width, r + PAD); b = min(im.height, b + PAD)
        return im.crop((l, t, r, b))
    return im


def rounded(im, radius=RADIUS, border=BORDER):
    im = im.convert("RGBA")
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    if border:
        bd = ImageDraw.Draw(out)
        bd.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius,
                             outline=BORDER_COLOR, width=border)
    return out


for f in glob.glob(os.path.join(SRC, "*.png")):
    im = Image.open(f)
    im = autocrop_white(im)
    im = rounded(im)
    im.save(f)
    print(os.path.basename(f), im.size)
print("done")
