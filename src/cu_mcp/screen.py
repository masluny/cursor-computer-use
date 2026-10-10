"""Screen capture, screenshot <-> screen coordinate mapping, and overlays.

The model always works in *screenshot pixels*: the main display downscaled so
its long edge is at most CU_MAX_EDGE (default 1280). macOS input events work in
*points* (logical pixels). `Geometry` converts between the two.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
from dataclasses import dataclass

import Quartz
from PIL import Image, ImageChops, ImageDraw, ImageFont

MAX_EDGE = int(os.environ.get("CU_MAX_EDGE", "1280"))
IMAGE_FORMAT = os.environ.get("CU_IMAGE_FORMAT", "jpeg").lower()  # jpeg | png
JPEG_QUALITY = int(os.environ.get("CU_JPEG_QUALITY", "80"))
FAST_CAPTURE = os.environ.get("CU_FAST_CAPTURE", "1") != "0"


@dataclass(frozen=True)
class Geometry:
    """Main display in global points plus the size of the image shown to the model."""

    origin_x: float
    origin_y: float
    width_pt: float
    height_pt: float
    img_w: int
    img_h: int

    @property
    def scale(self) -> float:
        """Screenshot pixels per point."""
        return self.img_w / self.width_pt

    def to_points(self, x: float, y: float) -> tuple[float, float]:
        """Screenshot pixel -> global point (clamped to the display)."""
        px = min(max(x, 0), self.img_w - 1) / self.scale
        py = min(max(y, 0), self.img_h - 1) / self.scale
        return self.origin_x + px, self.origin_y + py

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        """Global point -> screenshot pixel."""
        return round((x - self.origin_x) * self.scale), round((y - self.origin_y) * self.scale)

    def contains_point(self, x: float, y: float) -> bool:
        return (self.origin_x <= x < self.origin_x + self.width_pt
                and self.origin_y <= y < self.origin_y + self.height_pt)


def fit_size(width: float, height: float, max_edge: int = MAX_EDGE) -> tuple[int, int]:
    """Size of the downscaled image; never upscales."""
    s = min(1.0, max_edge / max(width, height))
    return round(width * s), round(height * s)


_work: dict = {"display": None}  # display the agent works on; None = the main display


def set_work_display(display_id: int | None) -> None:
    """Background mode points everything (screenshots, coordinates, OCR) at the hidden display."""
    _work["display"] = display_id


def display_bounds():
    """CGRect of the display the agent works on, in global points."""
    did = _work["display"]
    return Quartz.CGDisplayBounds(Quartz.CGMainDisplayID() if did is None else did)


def geometry() -> Geometry:
    b = display_bounds()
    w, h = b.size.width, b.size.height
    img_w, img_h = fit_size(w, h)
    return Geometry(b.origin.x, b.origin.y, w, h, img_w, img_h)


def has_screen_permission() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def _screencapture(extra_args: list[str]) -> Image.Image:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="cu_")
    os.close(fd)
    try:
        proc = subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", *extra_args, path],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0 or os.path.getsize(path) == 0:
            raise RuntimeError(f"screencapture failed: {proc.stderr.strip() or 'empty image'}. "
                               "Grant Screen Recording permission to the app running this server (Cursor).")
        with Image.open(path) as im:
            return im.convert("RGB")
    finally:
        os.unlink(path)


def cg_image(rect=None, best: bool = True):
    """In-process capture of a rect in global points (default: main display) as a CGImage, or None.

    ~10-20 ms versus ~280 ms for the screencapture CLI. Overlay windows with NSWindowSharingNone are left out,
    same as with screencapture."""
    if not FAST_CAPTURE:
        return None
    if rect is None:
        rect = display_bounds()
    opt = Quartz.kCGWindowImageBestResolution if best else Quartz.kCGWindowImageNominalResolution
    try:
        img = Quartz.CGWindowListCreateImage(rect, Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, opt)
    except Exception:  # noqa: BLE001 - the API is deprecated; fall back to the CLI
        return None
    if img is None or Quartz.CGImageGetWidth(img) == 0:
        return None
    return img


def _to_pil(img) -> Image.Image:
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", Quartz.CGImageGetBytesPerRow(img), 1)


def capture(geo: Geometry) -> Image.Image:
    """The work display (normally the main one), resized to the model's coordinate space."""
    img = cg_image(best=True)
    if img is not None:
        im = _to_pil(img).convert("RGB")
    elif _work["display"] is None:
        im = _screencapture(["-m"])
    else:
        im = _screencapture([f"-R{geo.origin_x:.0f},{geo.origin_y:.0f},{geo.width_pt:.0f},{geo.height_pt:.0f}"])
    return im.resize((geo.img_w, geo.img_h), Image.Resampling.LANCZOS)


def capture_region(x: float, y: float, w: float, h: float, max_edge: int = 1280) -> Image.Image:
    """Region in global points at native resolution, scaled so the long edge is max_edge."""
    img = cg_image(Quartz.CGRectMake(x, y, max(w, 1), max(h, 1)), best=True)
    if img is not None:
        im = _to_pil(img).convert("RGB")
    else:
        im = _screencapture([f"-R{x:.0f},{y:.0f},{max(w, 1):.0f},{max(h, 1):.0f}"])
    s = max_edge / max(im.size)
    s = min(s, 4.0)  # upscaling past 4x adds nothing
    if abs(s - 1) > 0.01:
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.LANCZOS)
    return im


# ---------------------------------------------------------------- change detection

THUMB = (160, 104)


def thumb_of(im: Image.Image) -> bytes:
    return im.convert("L").resize(THUMB, Image.Resampling.BILINEAR).tobytes()


def live_thumb() -> bytes | None:
    """Tiny grayscale frame of the work display for "has anything moved?" checks (~15 ms)."""
    img = cg_image(best=False)
    if img is None:
        return None
    return thumb_of(_to_pil(img))


def changed_bbox(a: bytes | None, b: bytes | None) -> tuple[int, int, int, int] | None:
    """Bounding box (thumbnail pixels) of the noticeably changed area, or None."""
    if not a or not b or len(a) != len(b):
        return None
    w = THUMB[0]
    xs, ys = [], []
    for i, (x, y) in enumerate(zip(a, b)):
        if abs(x - y) > 18:
            xs.append(i % w)
            ys.append(i // w)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def fine_changes(a: Image.Image | None, b: Image.Image | None, cell: int = 32,
                 max_boxes: int = 3) -> list[tuple[int, int, int, int]]:
    """Areas (image pixels) where two same-size screenshots really differ, merged per neighbouring grid cells, at
    most max_boxes, biggest first. For the changes a thumbnail averages away: a moved map pin, a changed number.
    A blinking text caret (a thin line) and the menu bar clock are ignored."""
    if a is None or b is None or a.size != b.size:
        return []
    r, g, bl = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).split()
    # the strongest channel: a red pin on a blue map barely changes brightness
    diff = ImageChops.lighter(ImageChops.lighter(r, g), bl).point(lambda v: 255 if v > 24 else 0)
    w, h = diff.size
    top = round(h * 0.035)  # menu bar
    means = diff.reduce(cell)  # average per cell: a quick pass, then an exact look at the cells that moved
    hot = set()
    for r in range(means.height):
        for c in range(means.width):
            if not means.getpixel((c, r)):
                continue
            box = (c * cell, max(r * cell, top), min(w, (c + 1) * cell), min(h, (r + 1) * cell))
            if box[1] >= box[3]:
                continue
            region = diff.crop(box)
            if region.histogram()[255] >= 6:
                bb = region.getbbox()
                if bb and (bb[2] - bb[0] > 3 or bb[3] - bb[1] <= 3):  # not a caret: a 1-3 px wide vertical bar
                    hot.add((c, r))
    boxes = []
    while hot:
        stack, group = [hot.pop()], []
        while stack:
            c, r = stack.pop()
            group.append((c, r))
            for n in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)):
                if n in hot:
                    hot.remove(n)
                    stack.append(n)
        x0 = min(c for c, _ in group) * cell
        y0 = min(r for _, r in group) * cell
        boxes.append((len(group), (x0, y0, min(w, (max(c for c, _ in group) + 1) * cell),
                                   min(h, (max(r for _, r in group) + 1) * cell))))
    return [b for _, b in sorted(boxes, key=lambda x: -x[0])[:max_boxes]]


def changed_fraction(a: bytes | None, b: bytes | None) -> float:
    """Share of thumbnail pixels that differ noticeably; 1.0 when either frame is missing."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 18) / len(a)


def encode(im: Image.Image) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if IMAGE_FORMAT == "png":
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "png"
    im.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), "jpeg"


def _font(size: int) -> ImageFont.ImageFont:
    for path in ("/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_grid(im: Image.Image, step: int = 100) -> Image.Image:
    """Faint coordinate grid with labels every `step` px, to help the model locate targets."""
    base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(11)
    for x in range(step, base.width, step):
        d.line([(x, 0), (x, base.height)], fill=(255, 0, 80, 90), width=1)
        d.text((x + 2, 2), str(x), font=font, fill=(255, 0, 80, 230), stroke_width=2, stroke_fill=(255, 255, 255, 230))
    for y in range(step, base.height, step):
        d.line([(0, y), (base.width, y)], fill=(255, 0, 80, 90), width=1)
        d.text((2, y + 2), str(y), font=font, fill=(255, 0, 80, 230), stroke_width=2, stroke_fill=(255, 255, 255, 230))
    return Image.alpha_composite(base, layer).convert("RGB")


def nice_step(span: float, target_lines: int = 8) -> int:
    """A round grid step (1, 2, 5, 10, 20, 25, 50, 100...) giving about target_lines lines over span."""
    raw = max(span / target_lines, 1)
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500):
        if step >= raw:
            return step
    return 1000


def draw_grid_mapped(im: Image.Image, x0: float, y0: float, x1: float, y1: float) -> Image.Image:
    """Grid on a zoomed crop whose lines are labeled in FULL-screenshot coordinates, so the model can click
    exactly what it sees in the zoom with click(x, y)."""
    base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(max(11, base.width // 90))
    sx, sy = base.width / (x1 - x0), base.height / (y1 - y0)
    step = nice_step(x1 - x0)
    for gx in range(int(x0 // step + 1) * step, int(x1) + 1, step):
        px = (gx - x0) * sx
        d.line([(px, 0), (px, base.height)], fill=(255, 0, 80, 110), width=1)
        d.text((px + 3, 3), str(gx), font=font, fill=(255, 0, 80, 240), stroke_width=2, stroke_fill=(255, 255, 255, 230))
    step = nice_step(y1 - y0, max(2, round(8 * (y1 - y0) / max(x1 - x0, 1))))
    for gy in range(int(y0 // step + 1) * step, int(y1) + 1, step):
        py = (gy - y0) * sy
        d.line([(0, py), (base.width, py)], fill=(255, 0, 80, 110), width=1)
        d.text((3, py + 3), str(gy), font=font, fill=(255, 0, 80, 240), stroke_width=2, stroke_fill=(255, 255, 255, 230))
    return Image.alpha_composite(base, layer).convert("RGB")


_MARK_COLORS = [(230, 25, 75), (0, 130, 200), (60, 180, 75), (245, 130, 48), (145, 30, 180),
                (0, 128, 128), (240, 50, 230), (128, 0, 0)]


def draw_marks(im: Image.Image, marks: list[tuple[int, tuple[int, int, int, int]]]) -> Image.Image:
    """Numbered boxes ("set of marks") over UI elements. marks = [(id, (x0, y0, x1, y1)), ...]."""
    base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(11)
    for mark_id, (x0, y0, x1, y1) in marks:
        color = _MARK_COLORS[mark_id % len(_MARK_COLORS)]
        d.rectangle([x0, y0, x1, y1], outline=color + (220,), width=2)
        label = str(mark_id)
        tx0, ty0, tx1, ty1 = d.textbbox((0, 0), label, font=font)
        tw, th = tx1 - tx0 + 6, ty1 - ty0 + 4
        lx, ly = x0, y0 - th if y0 - th >= 0 else y0
        d.rectangle([lx, ly, lx + tw, ly + th], fill=color + (235,))
        d.text((lx + 3 - tx0, ly + 2 - ty0), label, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, layer).convert("RGB")
