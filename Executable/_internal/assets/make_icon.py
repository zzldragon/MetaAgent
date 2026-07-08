"""Generate the MetaAgent application icon.

The icon is a dark, studio-style rounded tile (matching the app's dark theme,
base ``#1e1f24`` + blue accent ``#42A5F5``) with a glowing blue *node graph*
shaped like the letter "M" — the core metaphor of MetaAgent: a visual canvas
where agents are wired together as nodes and edges.

Run:
    python assets/make_icon.py

Outputs (next to this script):
    MetaAgent.ico   multi-resolution Windows icon (16..256 px) for the exe build
    MetaAgent.png   1024 px preview / source raster

Only depends on Pillow (already a runtime dependency for vision features).
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter

# ---- palette -------------------------------------------------------------
BG_TOP = (38, 42, 51)        # #262a33  studio navy (top)
BG_BOTTOM = (18, 19, 24)     # #121318  near-black (bottom)
ACCENT = (66, 165, 245)      # #42A5F5  app accent blue
ACCENT_DEEP = (30, 111, 208)  # #1E6FD0 deeper blue (node rims / edges)
HILITE = (150, 214, 255)     # #96D6FF  bright cyan highlight
GLOW = (56, 140, 230)        # blue glow behind the graph

# ---- geometry (in 1024-px design space) ----------------------------------
BASE = 1024
S = 4                        # supersample factor for anti-aliasing
SIZE = BASE * S

MARGIN = 44 * S              # transparent breathing room around the tile
RADIUS = 220 * S             # tile corner radius (~squircle)

# "M" node positions (design space, then scaled by S)
_M = [
    (300, 706),   # p1 bottom-left
    (356, 318),   # p2 top-left
    (512, 566),   # p3 middle valley (the hub)
    (668, 318),   # p4 top-right
    (724, 706),   # p5 bottom-right
]
NODES = [(x * S, y * S) for x, y in _M]
EDGES = [(0, 1), (1, 2), (2, 3), (3, 4)]

EDGE_W = 30 * S
NODE_R = 48 * S
HUB_R = 60 * S               # centre node is the "meta" hub — a touch larger


def _vgradient(w: int, h: int, top, bottom) -> Image.Image:
    """Vertical linear gradient, built cheaply as a 1-px column then stretched."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / (h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return col.resize((w, h))


def _rounded_mask(w: int, h: int, box, radius: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def _node(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, *, hub: bool) -> None:
    """A filled circular node with a deep-blue rim, brighter core and a spec
    highlight, so it reads as a glossy graph node rather than a flat dot."""
    def circle(c, rr, fill):
        draw.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=fill)

    circle((cx, cy), r, ACCENT_DEEP)                         # rim
    circle((cx, cy), int(r * 0.82), HILITE if hub else ACCENT)  # body
    circle((cx, cy), int(r * 0.52), HILITE)                  # bright core
    # small offset specular highlight
    hr = int(r * 0.24)
    circle((cx - int(r * 0.30), cy - int(r * 0.32)), hr, (245, 251, 255))


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 1) rounded tile with a vertical gradient fill
    tile_box = [MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN]
    grad = _vgradient(SIZE, SIZE, BG_TOP, BG_BOTTOM).convert("RGBA")
    tile_mask = _rounded_mask(SIZE, SIZE, tile_box, RADIUS)
    img.paste(grad, (0, 0), tile_mask)

    # subtle inner top sheen for depth
    sheen = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).rounded_rectangle(
        [MARGIN, MARGIN, SIZE - MARGIN, MARGIN + int(SIZE * 0.5)],
        radius=RADIUS, fill=(255, 255, 255, 16))
    sheen = sheen.filter(ImageFilter.GaussianBlur(20 * S))
    img = Image.alpha_composite(img, _clip(sheen, tile_mask))

    # 2) soft radial blue glow behind the graph
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gcx, gcy = 512 * S, 500 * S
    gr = 300 * S
    gd.ellipse([gcx - gr, gcy - gr, gcx + gr, gcy + gr], fill=GLOW + (150,))
    glow = glow.filter(ImageFilter.GaussianBlur(90 * S))
    img = Image.alpha_composite(img, _clip(glow, tile_mask))

    # 3) edges — a blurred glow pass, then a crisp stroke on top
    edges = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ed = ImageDraw.Draw(edges)
    for a, b in EDGES:
        ed.line([NODES[a], NODES[b]], fill=ACCENT + (255,),
                width=EDGE_W, joint="curve")
    edge_glow = edges.filter(ImageFilter.GaussianBlur(14 * S))
    img = Image.alpha_composite(img, _clip(edge_glow, tile_mask))
    img = Image.alpha_composite(img, _clip(edges, tile_mask))

    # 4) nodes on top
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i, (cx, cy) in enumerate(NODES):
        _node(ld, cx, cy, HUB_R if i == 2 else NODE_R, hub=(i == 2))
    img = Image.alpha_composite(img, _clip(layer, tile_mask))

    return img.resize((BASE, BASE), Image.LANCZOS)


def _clip(layer: Image.Image, mask: Image.Image) -> Image.Image:
    """Restrict an RGBA layer to the rounded-tile mask (keeps glows inside)."""
    out = layer.copy()
    a = out.getchannel("A")
    out.putalpha(Image.composite(a, Image.new("L", a.size, 0), mask))
    return out


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    icon = build()
    png_path = os.path.join(here, "MetaAgent.png")
    ico_path = os.path.join(here, "MetaAgent.ico")
    icon.save(png_path)
    icon.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                               (64, 64), (128, 128), (256, 256)])
    print("wrote", png_path)
    print("wrote", ico_path)


if __name__ == "__main__":
    main()
