# -*- coding: utf-8 -*-
"""Preview rendering: turns a plan into a picture the user can judge at a glance.

A 1240x11000 image scaled into a preview panel is a 50 px wide hair, so both
views wrap: the source is laid out as several columns read left to right, the
pieces as a grid. The layout that makes the content biggest wins.

Everything is drawn with Pillow into an image exactly the size of the canvas, so
the GUI can blit it at (0, 0) and put crisp Tk text on top using the returned
label coordinates.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

from . import core

BG = (38, 40, 44)
FRAME = (90, 94, 102)
LINE = (255, 64, 56)
OVERLAP = (255, 196, 0, 80)
SHADE = (0, 0, 0, 50)
PAD_FILL = (250, 250, 250)

MODE_SOURCE = "source"
MODE_PARTS = "parts"

GAP = 8
PAD = 8
LABEL_ROOM = 14

Label = Dict[str, object]


def render(proxy: Image.Image, plan: core.Plan, box_w: int, box_h: int,
           mode: str = MODE_SOURCE) -> Tuple[Image.Image, List[Label]]:
    box_w = max(60, int(box_w))
    box_h = max(60, int(box_h))
    if mode == MODE_PARTS:
        return _render_parts(proxy, plan, box_w, box_h)
    return _render_source(proxy, plan, box_w, box_h)


def make_proxy(img: Image.Image, max_side: int = 1800) -> Image.Image:
    """Cheap stand-in for the full image: previews never touch the original."""
    long_side = max(img.width, img.height)
    source = img.convert("RGB") if img.mode != "RGB" else img
    if long_side <= max_side:
        return source.copy() if source is img else source
    k = max_side / float(long_side)
    size = (max(1, int(round(img.width * k))), max(1, int(round(img.height * k))))
    return source.resize(size, Image.LANCZOS)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _chunks(items: Sequence, per: int) -> List[List]:
    return [list(items[i:i + per]) for i in range(0, len(items), per)]


def _scale_for(cols: int, rows: int, tile_w: float, tile_h: float,
               box_w: int, box_h: int, extra_w: int = 0, extra_h: int = 0) -> float:
    free_w = box_w - 2 * PAD - GAP * (cols - 1) - extra_w * cols
    free_h = box_h - 2 * PAD - GAP * (rows - 1) - extra_h * rows
    if free_w <= 0 or free_h <= 0 or tile_w <= 0 or tile_h <= 0:
        return 0.0
    return min(free_w / (cols * tile_w), free_h / (rows * tile_h))


def _crop_scaled(src: Image.Image, plan: core.Plan, start: int, end: int,
                 scale: float, out_w: int, out_h: int) -> Image.Image:
    """Crop a range along the cut axis out of the proxy and resize it."""
    if plan.axis == core.AXIS_Y:
        box = (0, int(start * scale), src.width, int(round(end * scale)))
    else:
        box = (int(start * scale), 0, int(round(end * scale)), src.height)
    box = (max(0, box[0]), max(0, box[1]),
           min(src.width, max(box[0] + 1, box[2])),
           min(src.height, max(box[1] + 1, box[3])))
    return src.crop(box).resize((max(1, out_w), max(1, out_h)), Image.LANCZOS)


# --------------------------------------------------------------------------
# source view: the whole image, wrapped into columns, with the cuts drawn on it
# --------------------------------------------------------------------------

def _render_source(proxy: Image.Image, plan: core.Plan,
                   box_w: int, box_h: int) -> Tuple[Image.Image, List[Label]]:
    count = max(1, plan.count)
    scale = (proxy.width / float(plan.width) if plan.axis == core.AXIS_Y
             else proxy.height / float(plan.height))
    if plan.axis == core.AXIS_X:
        scale = proxy.width / float(plan.width)
    thickness = plan.thickness

    # Pick how many columns (rows, for a wide image) to wrap the strip into.
    best = (1, 0.0, [])
    for columns in range(1, min(count, 12) + 1):
        per = int(math.ceil(count / float(columns)))
        groups = _chunks(plan.pieces, per)
        spans = _group_spans(groups, plan)
        longest = max(end - start for start, end in spans)
        if plan.axis == core.AXIS_Y:
            k = _scale_for(len(groups), 1, thickness, longest, box_w, box_h)
        else:
            k = _scale_for(1, len(groups), longest, thickness, box_w, box_h)
        if k > best[1]:
            best = (len(groups), k, spans)
    columns, k, spans = best
    groups = _chunks(plan.pieces, int(math.ceil(count / float(columns))))

    canvas = Image.new("RGB", (box_w, box_h), BG)
    overlay = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    shade = ImageDraw.Draw(overlay)
    labels: List[Label] = []

    tiles = []
    for (start, end) in spans:
        if plan.axis == core.AXIS_Y:
            tiles.append((max(1, int(round(thickness * k))),
                          max(1, int(round((end - start) * k)))))
        else:
            tiles.append((max(1, int(round((end - start) * k))),
                          max(1, int(round(thickness * k)))))

    if plan.axis == core.AXIS_Y:
        total = sum(t[0] for t in tiles) + GAP * (len(tiles) - 1)
        x = (box_w - total) // 2
        y = (box_h - max(t[1] for t in tiles)) // 2
        origins = []
        for tile in tiles:
            origins.append((x, y))
            x += tile[0] + GAP
    else:
        total = sum(t[1] for t in tiles) + GAP * (len(tiles) - 1)
        x = (box_w - max(t[0] for t in tiles)) // 2
        y = (box_h - total) // 2
        origins = []
        for tile in tiles:
            origins.append((x, y))
            y += tile[1] + GAP

    bands = [(second.start, first.end)
             for first, second in zip(plan.pieces, plan.pieces[1:])
             if first.end > second.start]

    for group, (start, end), (tw, th), (ox, oy) in zip(groups, spans, tiles, origins):
        canvas.paste(_crop_scaled(proxy, plan, start, end, scale, tw, th), (ox, oy))
        for piece in group:
            a = _clamp_px(piece.start - start, end - start, k)
            b = _clamp_px(min(piece.end, end) - start, end - start, k)
            if piece.index % 2 == 0:
                shade.rectangle(_rect(plan, ox, oy, tw, th, a, b), fill=SHADE)
            labels.append(_centre_label(plan, ox, oy, tw, th, a, b, piece.index))
        # overlapping stretches, so the user sees what is duplicated; a band
        # straddling a column break shows up in both columns
        for lo, hi in bands:
            if hi <= start or lo >= end:
                continue
            a = _clamp_px(max(lo, start) - start, end - start, k)
            b = _clamp_px(min(hi, end) - start, end - start, k)
            if b > a:
                shade.rectangle(_rect(plan, ox, oy, tw, th, a, b), fill=OVERLAP)

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for group, (start, end), (tw, th), (ox, oy) in zip(groups, spans, tiles, origins):
        edges = set()
        for piece in group:
            if piece.start > start:
                edges.add(piece.start)
            if piece.end < end:
                edges.add(piece.end)
        for edge in edges:
            v = _clamp_px(edge - start, end - start, k)
            if plan.axis == core.AXIS_Y:
                draw.line((ox, oy + v, ox + tw - 1, oy + v), fill=LINE)
            else:
                draw.line((ox + v, oy, ox + v, oy + th - 1), fill=LINE)
        draw.rectangle((ox - 1, oy - 1, ox + tw, oy + th), outline=FRAME)
    return canvas, labels


def _group_spans(groups: Sequence[Sequence[core.Piece]],
                 plan: core.Plan) -> List[Tuple[int, int]]:
    """Column ranges that tile the image without repeating anything."""
    spans = []
    for i, group in enumerate(groups):
        start = group[0].start
        if i + 1 < len(groups):
            end = groups[i + 1][0].start
        else:
            end = plan.length
        spans.append((start, max(start + 1, end)))
    return spans


def _clamp_px(value: int, span: int, k: float) -> int:
    return int(round(max(0, min(span, value)) * k))


def _rect(plan: core.Plan, ox: int, oy: int, tw: int, th: int, a: int, b: int):
    if plan.axis == core.AXIS_Y:
        return (ox, oy + a, ox + tw - 1, oy + b)
    return (ox + a, oy, ox + b, oy + th - 1)


def _centre_label(plan: core.Plan, ox: int, oy: int, tw: int, th: int,
                  a: int, b: int, index: int) -> Label:
    if plan.axis == core.AXIS_Y:
        return {"x": ox + tw // 2, "y": oy + (a + b) // 2, "text": str(index),
                "size": 12}
    return {"x": ox + (a + b) // 2, "y": oy + th // 2, "text": str(index), "size": 12}


# --------------------------------------------------------------------------
# parts view: what actually lands on disk
# --------------------------------------------------------------------------

def _render_parts(proxy: Image.Image, plan: core.Plan,
                  box_w: int, box_h: int) -> Tuple[Image.Image, List[Label]]:
    count = max(1, plan.count)
    sizes = [plan.piece_size(p) for p in plan.pieces]
    cell_w = max(s[0] for s in sizes)
    cell_h = max(s[1] for s in sizes)
    scale = proxy.width / float(plan.width)

    best_cols, best_k = 1, 0.0
    for cols in range(1, count + 1):
        rows = int(math.ceil(count / float(cols)))
        k = _scale_for(cols, rows, cell_w, cell_h, box_w, box_h, 0, LABEL_ROOM)
        if k > best_k:
            best_cols, best_k = cols, k
    cols, k = best_cols, best_k
    rows = int(math.ceil(count / float(cols)))

    cw = max(1, int(round(cell_w * k)))
    ch = max(1, int(round(cell_h * k)))
    grid_w = cols * cw + GAP * (cols - 1)
    grid_h = rows * (ch + LABEL_ROOM) + GAP * (rows - 1)
    x0 = (box_w - grid_w) // 2
    y0 = (box_h - grid_h) // 2

    canvas = Image.new("RGB", (box_w, box_h), BG)
    draw = ImageDraw.Draw(canvas)
    labels: List[Label] = []

    for i, piece in enumerate(plan.pieces):
        col, row = i % cols, i // cols
        px = x0 + col * (cw + GAP)
        py = y0 + row * (ch + LABEL_ROOM + GAP)
        w, h = sizes[i]
        dw = max(1, int(round(w * k)))
        dh = max(1, int(round(h * k)))
        tile = Image.new("RGB", (dw, dh), PAD_FILL)
        content_w, content_h = dw, dh
        if piece.pad_to > piece.length:
            fraction = piece.length / float(piece.pad_to)
            if plan.axis == core.AXIS_Y:
                content_h = max(1, int(round(dh * fraction)))
            else:
                content_w = max(1, int(round(dw * fraction)))
        tile.paste(_crop_scaled(proxy, plan, piece.start, piece.end, scale,
                                content_w, content_h), (0, 0))
        canvas.paste(tile, (px, py))
        draw.rectangle((px - 1, py - 1, px + dw, py + dh), outline=FRAME)
        labels.append({"x": px + dw // 2, "y": py + dh + LABEL_ROOM // 2,
                       "text": str(piece.index), "size": 10})
    return canvas, labels
