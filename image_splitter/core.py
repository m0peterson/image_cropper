# -*- coding: utf-8 -*-
"""Core splitting logic.

No Tk here, no global mutable state: everything is a pure function over a
Settings object, so the GUI, the CLI and the tests all share one implementation.

Vocabulary used throughout:

* **axis**   -- ``"y"`` means the image is cut into horizontal strips stacked
  top to bottom (the normal case for a 1240x11000 banner); ``"x"`` means it is
  cut into vertical columns (for very wide images).
* **length** -- the size along the axis being cut.
* **thickness** -- the other side, which every piece keeps in full.
* **cut**    -- an internal boundary position, in source pixels.
* **piece**  -- a half-open range ``[start, end)`` along the axis.
"""

from __future__ import annotations

import math
import os
import re
import warnings
from dataclasses import dataclass, replace
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageOps

# Gigantic images are the whole point of this tool, so Pillow's decompression
# bomb guard is raised rather than removed: 1 Gpx still leaves a real ceiling.
Image.MAX_IMAGE_PIXELS = 1000000000
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

AXIS_Y = "y"
AXIS_X = "x"
AXIS_AUTO = "auto"

MODE_AUTO = "auto"      # pick the piece count that lands closest to the target ratio
MODE_COUNT = "count"    # user fixes the number of pieces, split evenly
MODE_EXACT = "exact"    # every piece exactly the target ratio, tail handled separately

TAIL_EXTEND = "extend"  # pull the last piece back so it is full size (extra overlap)
TAIL_KEEP = "keep"      # leave the short tail as is
TAIL_PAD = "pad"        # pad the short tail with background to the target ratio
TAIL_DROP = "drop"      # throw the short tail away

READABLE_EXT = (".png", ".jpg", ".jpeg", ".jpe", ".webp", ".bmp", ".tif", ".tiff",
                ".gif", ".ppm", ".tga", ".jfif")


@dataclass
class Settings:
    """Everything the planner needs. Plain values so it is trivial to serialise."""

    ratio_w: float = 4.0
    ratio_h: float = 5.0
    mode: str = MODE_AUTO
    count: int = 2
    overlap: int = 0            # pixels shared between neighbouring pieces
    smart: bool = True          # snap cuts to quiet rows
    smart_window: float = 0.12  # search radius, as a fraction of the nominal piece
    tail: str = TAIL_EXTEND
    min_piece: int = 16
    max_parts: int = 500
    axis: str = AXIS_AUTO

    def ratio(self) -> float:
        """Target width/height of a single piece."""
        return float(self.ratio_w) / float(self.ratio_h)


@dataclass
class Piece:
    index: int
    start: int
    end: int
    pad_to: int = 0  # >0: pad the long side up to this many pixels on save

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Plan:
    axis: str
    width: int
    height: int
    pieces: List[Piece]
    nominal: float
    note: str = ""

    @property
    def thickness(self) -> int:
        return self.width if self.axis == AXIS_Y else self.height

    @property
    def length(self) -> int:
        return self.height if self.axis == AXIS_Y else self.width

    @property
    def count(self) -> int:
        return len(self.pieces)

    def box(self, piece: Piece) -> Tuple[int, int, int, int]:
        """Crop box for Image.crop()."""
        if self.axis == AXIS_Y:
            return (0, piece.start, self.width, piece.end)
        return (piece.start, 0, piece.end, self.height)

    def piece_size(self, piece: Piece) -> Tuple[int, int]:
        """Size of the saved file, padding included."""
        long_side = piece.pad_to if piece.pad_to > piece.length else piece.length
        if self.axis == AXIS_Y:
            return (self.width, long_side)
        return (long_side, self.height)

    def piece_ratio(self, piece: Piece) -> float:
        w, h = self.piece_size(piece)
        return float(w) / float(h) if h else 0.0

    def cuts(self) -> List[int]:
        """Internal boundaries, ignoring overlap (useful for drawing)."""
        return [p.end for p in self.pieces[:-1]]


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def resolve_axis(width: int, height: int, axis: str = AXIS_AUTO) -> str:
    if axis in (AXIS_X, AXIS_Y):
        return axis
    return AXIS_Y if height >= width else AXIS_X


def nominal_piece(width: int, height: int, settings: Settings, axis: str) -> float:
    """Ideal size of one piece along the cut axis, in pixels."""
    rw = float(settings.ratio_w)
    rh = float(settings.ratio_h)
    if rw <= 0 or rh <= 0:
        raise ValueError("aspect ratio must be positive")
    if axis == AXIS_Y:
        return width * rh / rw
    return height * rw / rh


def _even_cuts(length: int, n: int) -> List[int]:
    return [int(round(i * length / float(n))) for i in range(1, n)]


def _sanitize(cuts: Iterable[int], length: int, min_piece: int) -> List[int]:
    """Sorted, unique, inside the image and never closer than min_piece."""
    out: List[int] = []
    for c in sorted(int(c) for c in cuts):
        if c < min_piece or c > length - min_piece:
            continue
        if out and c - out[-1] < min_piece:
            continue
        out.append(c)
    return out


def _materialize(cuts: Sequence[int], length: int, settings: Settings,
                 nominal: float) -> List[Piece]:
    overlap = max(0, int(settings.overlap))
    bounds = [0] + list(cuts) + [length]
    n = len(bounds) - 1
    raw: List[List[int]] = []
    for i in range(n):
        a, b = bounds[i], bounds[i + 1]
        if overlap > 0 and n > 1:
            # Every piece grows into its successor; the last one grows backwards
            # instead, so all pieces keep the same size.
            if i < n - 1:
                b = min(length, b + overlap)
            else:
                a = max(0, a - overlap)
        raw.append([a, b])

    pad_to = 0
    if settings.mode == MODE_EXACT and n > 1 and nominal > 0:
        target = int(round(nominal))
        if raw[-1][1] - raw[-1][0] < 0.9 * nominal:
            if settings.tail == TAIL_EXTEND:
                raw[-1][0] = max(0, length - target)
            elif settings.tail == TAIL_DROP:
                raw.pop()
            elif settings.tail == TAIL_PAD:
                pad_to = target

    # With a large overlap the pulled-back last piece can land entirely inside
    # its predecessor; writing the same crop twice helps nobody.
    cleaned: List[List[int]] = []
    for a, b in raw:
        while cleaned and a <= cleaned[-1][0] and b >= cleaned[-1][1]:
            cleaned.pop()               # this one swallows its predecessor
        if cleaned and a >= cleaned[-1][0] and b <= cleaned[-1][1]:
            continue                    # nothing new compared to its predecessor
        cleaned.append([a, b])

    pieces = [Piece(i + 1, a, b) for i, (a, b) in enumerate(cleaned)]
    if pad_to and pieces:
        pieces[-1].pad_to = pad_to
    return pieces


def plan_pieces(width: int, height: int, settings: Settings,
                profile: Optional[Sequence[float]] = None) -> Plan:
    """Work out where to cut. `profile` enables smart snapping when given."""
    if width <= 0 or height <= 0:
        raise ValueError("empty image")

    axis = resolve_axis(width, height, settings.axis)
    length = height if axis == AXIS_Y else width
    nominal = nominal_piece(width, height, settings, axis)
    min_piece = max(1, int(settings.min_piece))
    note = ""

    # Overlap past half a piece would make every piece reach the end of the
    # image, so it is capped instead of producing near-duplicate files.
    overlap = max(0, int(settings.overlap))
    overlap_cap = int(min(nominal, length) * 0.5)
    if overlap > overlap_cap:
        overlap = max(0, overlap_cap)
        note = "перекрытие уменьшено до половины части ({0} px)".format(overlap)
    settings = replace(settings, overlap=overlap)

    if nominal < min_piece and length > min_piece:
        note = ("такое соотношение недостижимо при этой ширине: "
                "части будут крупнее, чем нужно")

    if settings.mode == MODE_COUNT:
        n = max(1, min(int(settings.count), int(settings.max_parts)))
        if n > 1 and length // n < min_piece:
            n = max(1, length // min_piece)
            note = "число частей уменьшено: куски были бы слишком тонкими"
        cuts = _even_cuts(length, n)
    elif settings.mode == MODE_EXACT:
        spacing = int(round(nominal)) - overlap
        spacing = max(min_piece, spacing)
        n = max(1, int(math.ceil(length / float(spacing))))
        if n > settings.max_parts:
            n = int(settings.max_parts)
            spacing = max(min_piece, int(math.ceil(length / float(n))))
            note = "достигнут лимит числа частей"
        cuts = [i * spacing for i in range(1, n) if i * spacing < length]
    else:
        effective = nominal - overlap
        if effective < min_piece:
            effective = max(float(min_piece), nominal)
        n = int(round(length / effective)) if effective > 0 else 1
        n = max(1, min(n, int(settings.max_parts)))
        cuts = _even_cuts(length, n)

    cuts = _sanitize(cuts, length, min_piece)

    # A profile for the other axis (or a stale one) would silently produce
    # nonsense cuts, so it is simply ignored.
    if settings.smart and profile is not None and cuts and len(profile) >= length:
        window = int(round(max(4.0, nominal * float(settings.smart_window))))
        cuts = snap_cuts(cuts, profile, window, min_piece=min_piece, length=length)

    pieces = _materialize(cuts, length, settings, nominal)
    return Plan(axis=axis, width=width, height=height, pieces=pieces,
                nominal=nominal, note=note)


# --------------------------------------------------------------------------
# smart cuts
# --------------------------------------------------------------------------

def pixel_values(img: Image.Image) -> List[float]:
    """Flat list of samples. Pillow 12 renamed getdata(); support both names."""
    getter = getattr(img, "get_flattened_data", None)
    if getter is None:
        getter = img.getdata
    return list(getter())


def activity_profile(img: Image.Image, axis: str, samples: int = 96) -> List[float]:
    """Per-row "how much is going on here" score, one value per source row.

    Low score = the row looks like the one before it and is itself flat, i.e. a
    gap between paragraphs or a background band -- a good place to cut. The
    whole thing runs inside Pillow's C code, so it stays fast on 100k-px images.
    """
    gray = img.convert("L")
    if axis == AXIS_X:
        gray = gray.transpose(Image.ROTATE_90)  # columns become rows
    length = gray.height
    samples = max(8, min(int(samples), max(8, gray.width)))
    if length < 3:
        return [0.0] * length

    small = gray.resize((samples, length), Image.BOX)
    # difference against the previous row -> edges that a cut would slice through
    shifted = ImageChops.offset(small, 0, 1)
    step = ImageChops.difference(small, shifted).resize((1, length), Image.BOX)
    # deviation inside the row -> busy rows score higher than plain ones
    row_mean = small.resize((1, length), Image.BOX).resize((samples, length), Image.NEAREST)
    flat = ImageChops.difference(small, row_mean).resize((1, length), Image.BOX)

    step_data = pixel_values(step)
    flat_data = pixel_values(flat)
    profile = [float(step_data[i]) + 0.5 * float(flat_data[i]) for i in range(length)]
    # offset() wraps around, so row 0 is meaningless; never cut at the edges.
    profile[0] = 1e6
    profile[-1] = 1e6
    return profile


def snap_cuts(cuts: Sequence[int], profile: Sequence[float], window: int,
              min_piece: int = 16, length: Optional[int] = None,
              tolerance: float = 2.0) -> List[int]:
    """Move each cut onto a quiet row inside +/- window.

    Quietness wins first, distance breaks the tie: every row within `tolerance`
    of the calmest one in the window counts as equally good, so a blank gap two
    pixels away beats an equally blank gap two hundred pixels away. When the
    whole window is equally busy nothing moves.
    """
    n = len(profile)
    limit = int(length) if length else n
    out: List[int] = []
    for i, cut in enumerate(cuts):
        low = (out[-1] if out else 0) + min_piece
        high = (cuts[i + 1] if i + 1 < len(cuts) else limit) - min_piece
        a = int(max(low, cut - window, 1))
        b = int(min(high, cut + window, n - 1))
        if a > b:
            out.append(cut)
            continue
        calmest = min(profile[a:b + 1])
        ceiling = max(calmest + tolerance, calmest * 1.12)
        best = cut
        best_distance = None
        for r in range(a, b + 1):
            if profile[r] > ceiling:
                continue
            distance = abs(r - cut)
            if best_distance is None or distance < best_distance:
                best, best_distance = r, distance
        out.append(best)
    return out


# --------------------------------------------------------------------------
# producing the files
# --------------------------------------------------------------------------

def load_image(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def crop_piece(img: Image.Image, plan: Plan, piece: Piece,
               background: Tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    out = img.crop(plan.box(piece))
    if piece.pad_to > piece.length:
        target = plan.piece_size(piece)
        mode = out.mode if out.mode in ("RGB", "RGBA", "L") else "RGB"
        if out.mode != mode:
            out = out.convert(mode)
        fill: Tuple[int, ...] = background
        if mode == "RGBA":
            fill = background + (255,)
        elif mode == "L":
            fill = (int(sum(background) / 3),)
        canvas = Image.new(mode, target, fill if len(fill) > 1 else fill[0])
        canvas.paste(out, (0, 0))
        out = canvas
    return out


def _flatten(img: Image.Image, background: Tuple[int, int, int]) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def save_piece(img: Image.Image, path: str, fmt: str = "png", quality: int = 92,
               background: Tuple[int, int, int] = (255, 255, 255)) -> str:
    fmt = (fmt or "png").lower()
    if fmt in ("jpg", "jpeg"):
        _flatten(img, background).save(path, "JPEG", quality=int(quality),
                                       optimize=True, progressive=True,
                                       subsampling=0 if quality >= 90 else 2)
    elif fmt == "webp":
        src = img if img.mode in ("RGB", "RGBA") else img.convert("RGB")
        src.save(path, "WEBP", quality=int(quality), method=4)
    elif fmt in ("tif", "tiff"):
        img.save(path, "TIFF", compression="tiff_deflate")
    elif fmt == "bmp":
        _flatten(img, background).save(path, "BMP")
    else:
        src = img if img.mode in ("RGB", "RGBA", "L", "P", "LA") else img.convert("RGBA")
        src.save(path, "PNG", optimize=True)
    return path


EXT_BY_FORMAT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp",
                 "tiff": ".tif", "tif": ".tif", "bmp": ".bmp"}

_SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def pick_format(fmt: str, source_path: str) -> str:
    """`fmt == "same"` keeps the source container when we can write it."""
    if fmt and fmt != "same":
        return fmt.lower()
    ext = os.path.splitext(source_path)[1].lower().lstrip(".")
    if ext in ("jpg", "jpe", "jfif"):
        return "jpeg"
    if ext in EXT_BY_FORMAT or ext == "jpeg":
        return ext
    return "png"


def build_name(pattern: str, stem: str, index: int, total: int,
               width: int, height: int, ext: str) -> str:
    pattern = pattern or "{stem}_{i:02d}"
    try:
        name = pattern.format(stem=stem, i=index, n=total, w=width, h=height,
                              total=total, num=index)
    except (KeyError, IndexError, ValueError):
        name = "{0}_{1:02d}".format(stem, index)
    name = _SAFE.sub("_", name).strip() or "part_{0:02d}".format(index)
    if not name.lower().endswith(ext):
        name += ext
    return name


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("{0} ({1}){2}".format(base, i, ext)):
        i += 1
    return "{0} ({1}){2}".format(base, i, ext)


def default_output_dir(source_path: str) -> str:
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(source_path)), stem + "_parts")


def split_file(path: str, settings: Settings, out_dir: Optional[str] = None,
               fmt: str = "png", quality: int = 92, pattern: str = "{stem}_{i:02d}",
               background: Tuple[int, int, int] = (255, 255, 255),
               overwrite: bool = False,
               progress=None) -> List[str]:
    """Split one file and write the pieces. Returns the paths written.

    `progress(done, total, path)` is called after each piece; raising from it is
    how the GUI cancels a long run.
    """
    img = load_image(path)
    axis = resolve_axis(img.width, img.height, settings.axis)
    profile = activity_profile(img, axis) if settings.smart else None
    plan = plan_pieces(img.width, img.height, settings, profile)

    out_dir = out_dir or default_output_dir(path)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    real_fmt = pick_format(fmt, path)
    ext = EXT_BY_FORMAT.get(real_fmt, ".png")
    stem = os.path.splitext(os.path.basename(path))[0]
    written: List[str] = []
    total = len(plan.pieces)

    for piece in plan.pieces:
        w, h = plan.piece_size(piece)
        name = build_name(pattern, stem, piece.index, total, w, h, ext)
        target = os.path.join(out_dir, name)
        if not overwrite:
            target = unique_path(target)
        chunk = crop_piece(img, plan, piece, background)
        save_piece(chunk, target, real_fmt, quality, background)
        chunk.close()
        written.append(target)
        if progress is not None:
            progress(piece.index, total, target)

    img.close()
    return written


def describe_plan(plan: Plan) -> str:
    """One-line human summary used by both front ends."""
    if not plan.pieces:
        return "нечего резать"
    sizes = [plan.piece_size(p) for p in plan.pieces]
    ratios = [w / float(h) for w, h in sizes]
    first = sizes[0]
    same = all(s == first for s in sizes)
    axis_name = "по горизонтали" if plan.axis == AXIS_Y else "по вертикали"
    if same:
        body = "{0} x {1} px, соотношение {2}".format(first[0], first[1],
                                                      ratio_text(ratios[0]))
    else:
        body = "от {0}x{1} до {2}x{3}, соотношение {4:.2f}..{5:.2f}".format(
            min(s[0] for s in sizes), min(s[1] for s in sizes),
            max(s[0] for s in sizes), max(s[1] for s in sizes),
            min(ratios), max(ratios))
    return "{0} {1} {2}: {3}".format(len(plan.pieces), plural_parts(len(plan.pieces)),
                                     axis_name, body)


def plural_parts(n: int) -> str:
    """часть / части / частей -- Russian numerals, because 'част(ей)' looks sloppy."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "частей"
    tail = n % 10
    if tail == 1:
        return "часть"
    if tail in (2, 3, 4):
        return "части"
    return "частей"


def ratio_text(ratio: float) -> str:
    """0.8 -> "4:5 (0.80)" when it lands on a familiar ratio, else just the number."""
    known = [(1, 1), (4, 5), (3, 4), (2, 3), (9, 16), (5, 4), (4, 3), (3, 2),
             (16, 9), (21, 9), (5, 7), (7, 5), (2, 1), (1, 2), (185, 100)]
    for w, h in known:
        if abs(ratio - w / float(h)) < 0.012:
            return "{0}:{1} ({2:.2f})".format(w, h, ratio)
    return "{0:.2f}".format(ratio)
