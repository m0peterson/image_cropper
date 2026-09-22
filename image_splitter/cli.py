# -*- coding: utf-8 -*-
"""Command line front end -- same engine as the GUI, handy for batch jobs."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import core


def parse_ratio(text: str):
    text = (text or "").strip().replace(",", ".").lower().replace("x", ":")
    if ":" in text:
        left, _, right = text.partition(":")
        return float(left), float(right)
    value = float(text)          # "0.8" means width/height
    return value, 1.0


def parse_color(text: str):
    text = (text or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return (255, 255, 255)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image-splitter",
        description="Cut a long image into pieces with a usable aspect ratio.")
    parser.add_argument("files", nargs="+", help="image files to split")
    parser.add_argument("-r", "--ratio", default="4:5",
                        help="target aspect ratio of one piece, e.g. 4:5 (default 4:5)")
    parser.add_argument("-m", "--mode", default=core.MODE_AUTO,
                        choices=[core.MODE_AUTO, core.MODE_COUNT, core.MODE_EXACT])
    parser.add_argument("-n", "--count", type=int, default=2,
                        help="number of pieces for --mode count")
    parser.add_argument("--overlap", type=int, default=0,
                        help="pixels shared by neighbouring pieces")
    parser.add_argument("--no-smart", action="store_true",
                        help="cut exactly on the computed lines")
    parser.add_argument("--window", type=int, default=12,
                        help="smart search radius, %% of a piece (default 12)")
    parser.add_argument("--tail", default=core.TAIL_EXTEND,
                        choices=[core.TAIL_EXTEND, core.TAIL_KEEP, core.TAIL_PAD,
                                 core.TAIL_DROP])
    parser.add_argument("--axis", default=core.AXIS_AUTO,
                        choices=[core.AXIS_AUTO, core.AXIS_Y, core.AXIS_X])
    parser.add_argument("-o", "--out", default=None,
                        help="output folder (default: <name>_parts next to the source)")
    parser.add_argument("-f", "--format", default="png",
                        choices=["png", "jpeg", "webp", "tiff", "bmp", "same"])
    parser.add_argument("-q", "--quality", type=int, default=92)
    parser.add_argument("--pattern", default="{stem}_{i:02d}",
                        help="file name pattern, tokens: {stem} {i} {n} {w} {h}")
    parser.add_argument("--background", default="#ffffff",
                        help="fill colour for padding and for flattening alpha")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, write nothing")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ratio_w, ratio_h = parse_ratio(args.ratio)
    except ValueError:
        print("bad --ratio value: {0}".format(args.ratio), file=sys.stderr)
        return 2

    settings = core.Settings(
        ratio_w=ratio_w, ratio_h=ratio_h, mode=args.mode, count=args.count,
        overlap=args.overlap, smart=not args.no_smart,
        smart_window=max(1, args.window) / 100.0, tail=args.tail, axis=args.axis)
    background = parse_color(args.background)

    failures = 0
    for path in args.files:
        if not os.path.isfile(path):
            print("no such file: {0}".format(path), file=sys.stderr)
            failures += 1
            continue
        try:
            if args.dry_run:
                image = core.load_image(path)
                axis = core.resolve_axis(image.width, image.height, settings.axis)
                profile = core.activity_profile(image, axis) if settings.smart else None
                plan = core.plan_pieces(image.width, image.height, settings, profile)
                print("{0}: {1}x{2} -> {3}".format(os.path.basename(path), image.width,
                                                   image.height,
                                                   core.describe_plan(plan)))
                for piece in plan.pieces:
                    w, h = plan.piece_size(piece)
                    print("   {0:>3}. {1}..{2}  {3}x{4}  {5}".format(
                        piece.index, piece.start, piece.end, w, h,
                        core.ratio_text(w / float(h))))
                image.close()
                continue
            written = core.split_file(path, settings, args.out, args.format,
                                      args.quality, args.pattern, background,
                                      overwrite=args.overwrite)
            print("{0}: {1} piece(s) -> {2}".format(
                os.path.basename(path), len(written),
                os.path.dirname(written[0]) if written else "-"))
        except Exception as exc:
            print("{0}: FAILED: {1}".format(path, exc), file=sys.stderr)
            failures += 1
    return 1 if failures else 0
