# -*- coding: utf-8 -*-
"""Plain unittest so it runs anywhere: python -m unittest discover -s tests"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from image_splitter import core  # noqa: E402


def plan_for(w, h, **kwargs):
    settings = core.Settings(**kwargs)
    return core.plan_pieces(w, h, settings)


class TestAxis(unittest.TestCase):
    def test_tall_image_cuts_into_rows(self):
        self.assertEqual(core.resolve_axis(1240, 11000), core.AXIS_Y)

    def test_wide_image_cuts_into_columns(self):
        self.assertEqual(core.resolve_axis(11000, 1240), core.AXIS_X)

    def test_forced_axis_wins(self):
        self.assertEqual(core.resolve_axis(1240, 11000, core.AXIS_X), core.AXIS_X)


class TestPlanAuto(unittest.TestCase):
    def test_pieces_cover_the_whole_image_without_gaps(self):
        plan = plan_for(1240, 11000, smart=False)
        self.assertEqual(plan.pieces[0].start, 0)
        self.assertEqual(plan.pieces[-1].end, 11000)
        for a, b in zip(plan.pieces, plan.pieces[1:]):
            self.assertEqual(a.end, b.start)

    def test_ratio_lands_near_the_target(self):
        plan = plan_for(1240, 11000, smart=False)          # target 4:5 = 0.80
        for piece in plan.pieces:
            self.assertAlmostEqual(plan.piece_ratio(piece), 0.8, delta=0.12)

    def test_piece_count_is_the_nearest_whole_number(self):
        plan = plan_for(1240, 11000, smart=False)          # 11000 / 1550 = 7.1
        self.assertEqual(plan.count, 7)

    def test_already_fine_ratio_is_left_alone(self):
        plan = plan_for(1000, 1250, smart=False)
        self.assertEqual(plan.count, 1)
        self.assertEqual(plan.pieces[0].start, 0)
        self.assertEqual(plan.pieces[0].end, 1250)

    def test_wide_image_is_cut_vertically(self):
        plan = plan_for(11000, 1240, ratio_w=5, ratio_h=4, smart=False)
        self.assertEqual(plan.axis, core.AXIS_X)
        self.assertEqual(plan.pieces[-1].end, 11000)
        for piece in plan.pieces:
            self.assertAlmostEqual(plan.piece_ratio(piece), 1.25, delta=0.15)

    def test_square_target_on_a_tall_strip(self):
        plan = plan_for(500, 5000, ratio_w=1, ratio_h=1, smart=False)
        self.assertEqual(plan.count, 10)
        self.assertEqual(plan.piece_size(plan.pieces[0]), (500, 500))


class TestPlanCount(unittest.TestCase):
    def test_exact_number_of_pieces(self):
        plan = plan_for(1240, 11000, mode=core.MODE_COUNT, count=4, smart=False)
        self.assertEqual(plan.count, 4)
        self.assertEqual(sum(p.length for p in plan.pieces), 11000)

    def test_one_piece_is_allowed(self):
        plan = plan_for(1240, 11000, mode=core.MODE_COUNT, count=1, smart=False)
        self.assertEqual(plan.count, 1)

    def test_absurd_count_is_clamped_to_something_sane(self):
        plan = plan_for(100, 300, mode=core.MODE_COUNT, count=400, smart=False)
        self.assertLessEqual(plan.count, 300 // 16 + 1)
        self.assertTrue(plan.note)


class TestPlanExact(unittest.TestCase):
    def test_every_piece_has_the_target_size(self):
        plan = plan_for(1240, 11000, mode=core.MODE_EXACT, smart=False)
        for piece in plan.pieces:
            self.assertEqual(piece.length, 1550)

    def test_tail_keep_leaves_a_short_piece(self):
        plan = plan_for(1240, 11000, mode=core.MODE_EXACT, tail=core.TAIL_KEEP,
                        smart=False)
        self.assertEqual(plan.pieces[-1].length, 11000 - 7 * 1550)
        self.assertEqual(plan.pieces[-1].end, 11000)

    def test_tail_pad_reports_the_padded_size(self):
        plan = plan_for(1240, 11000, mode=core.MODE_EXACT, tail=core.TAIL_PAD,
                        smart=False)
        last = plan.pieces[-1]
        self.assertEqual(plan.piece_size(last), (1240, 1550))
        self.assertAlmostEqual(plan.piece_ratio(last), 0.8, delta=0.001)

    def test_tail_drop_loses_the_remainder(self):
        keep = plan_for(1240, 11000, mode=core.MODE_EXACT, tail=core.TAIL_KEEP,
                        smart=False)
        drop = plan_for(1240, 11000, mode=core.MODE_EXACT, tail=core.TAIL_DROP,
                        smart=False)
        self.assertEqual(drop.count, keep.count - 1)
        self.assertLess(drop.pieces[-1].end, 11000)

    def test_exact_mode_on_an_evenly_divisible_image(self):
        plan = plan_for(1000, 5000, ratio_w=1, ratio_h=1, mode=core.MODE_EXACT,
                        smart=False)
        self.assertEqual(plan.count, 5)
        self.assertEqual(plan.pieces[-1].end, 5000)


class TestOverlap(unittest.TestCase):
    def test_neighbours_share_pixels(self):
        plan = plan_for(1240, 11000, overlap=80, smart=False)
        for a, b in zip(plan.pieces, plan.pieces[1:]):
            self.assertGreater(a.end, b.start)

    def test_all_pieces_keep_the_same_size(self):
        plan = plan_for(1240, 11000, overlap=80, smart=False)
        sizes = set(p.length for p in plan.pieces)
        self.assertLessEqual(len(sizes), 2)          # rounding may differ by 1 px
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_coverage_is_complete(self):
        plan = plan_for(1240, 11000, overlap=120, smart=False)
        self.assertEqual(plan.pieces[0].start, 0)
        self.assertEqual(plan.pieces[-1].end, 11000)
        covered = 0
        cursor = 0
        for piece in plan.pieces:
            self.assertLessEqual(piece.start, cursor)
            cursor = max(cursor, piece.end)
            covered = cursor
        self.assertEqual(covered, 11000)

    def test_overlap_bigger_than_the_piece_is_capped(self):
        plan = plan_for(1240, 11000, overlap=99999, smart=False)
        self.assertGreaterEqual(plan.count, 1)
        self.assertEqual(plan.pieces[-1].end, 11000)
        self.assertTrue(plan.note)
        starts = [p.start for p in plan.pieces]
        ends = [p.end for p in plan.pieces]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(ends)), len(ends))        # no duplicate pieces
        longest = max(p.length for p in plan.pieces)
        self.assertLessEqual(longest, plan.nominal * 1.6)

    def test_no_piece_repeats_its_neighbour(self):
        for width, height, overlap in ((500, 5000, 100000), (1240, 11000, 99999),
                                       (300, 2000, 700), (800, 9000, 5000)):
            plan = plan_for(width, height, overlap=overlap, smart=False)
            ranges = [(p.start, p.end) for p in plan.pieces]
            self.assertEqual(len(set(ranges)), len(ranges), ranges)
            for first, second in zip(plan.pieces, plan.pieces[1:]):
                self.assertGreater(second.start, first.start, ranges)
                self.assertGreater(second.end, first.end, ranges)
            self.assertEqual(plan.pieces[0].start, 0)
            self.assertEqual(plan.pieces[-1].end, height)

    def test_unreachable_ratio_is_flagged(self):
        plan = plan_for(4, 900, smart=False)                # 4 px wide, target 4:5
        self.assertTrue(plan.note)


class TestSmartCuts(unittest.TestCase):
    def build_striped(self):
        """Noisy blocks separated by flat white gaps at known positions."""
        w, h = 300, 3000
        img = Image.new("L", (w, h), 255)
        gaps = []
        y = 0
        block = 0
        while y < h:
            top = y
            bottom = min(h, y + 260)
            for yy in range(top, bottom):
                for xx in range(0, w, 3):
                    img.putpixel((xx, yy), (yy * 7 + xx * 13 + block) % 255)
            gaps.append((bottom, min(h, bottom + 40)))
            y = bottom + 40
            block += 1
        return img, gaps

    def test_cut_moves_into_a_blank_gap(self):
        img, gaps = self.build_striped()
        profile = core.activity_profile(img, core.AXIS_Y)
        settings = core.Settings(ratio_w=1, ratio_h=1, smart=True, smart_window=0.2)
        plan = core.plan_pieces(img.width, img.height, settings, profile)
        for cut in plan.cuts():
            self.assertTrue(any(a - 4 <= cut <= b + 4 for a, b in gaps),
                            "cut at {0} is not inside a blank gap".format(cut))

    def test_profile_length_matches_the_axis(self):
        img = Image.new("RGB", (120, 900), "white")
        self.assertEqual(len(core.activity_profile(img, core.AXIS_Y)), 900)
        self.assertEqual(len(core.activity_profile(img, core.AXIS_X)), 120)

    def test_snapping_keeps_cuts_ordered_and_apart(self):
        profile = [0.0] * 1000
        cuts = core.snap_cuts([300, 320], profile, window=200, min_piece=16, length=1000)
        self.assertEqual(sorted(cuts), cuts)
        self.assertGreaterEqual(cuts[1] - cuts[0], 16)


class TestNaming(unittest.TestCase):
    def test_default_pattern(self):
        self.assertEqual(core.build_name("{stem}_{i:02d}", "banner", 3, 9, 100, 200,
                                         ".png"), "banner_03.png")

    def test_tokens(self):
        self.assertEqual(core.build_name("{stem}-{i}of{n}-{w}x{h}", "a", 2, 5, 10, 20,
                                         ".jpg"), "a-2of5-10x20.jpg")

    def test_broken_pattern_falls_back(self):
        self.assertEqual(core.build_name("{nope}", "a", 1, 2, 3, 4, ".png"),
                         "a_01.png")

    def test_illegal_characters_are_scrubbed(self):
        name = core.build_name("a/b:c", "x", 1, 1, 1, 1, ".png")
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)

    def test_format_choice(self):
        self.assertEqual(core.pick_format("same", "x.JPG"), "jpeg")
        self.assertEqual(core.pick_format("same", "x.heic"), "png")
        self.assertEqual(core.pick_format("webp", "x.png"), "webp")


class TestRatioText(unittest.TestCase):
    def test_known_ratio(self):
        self.assertTrue(core.ratio_text(0.8).startswith("4:5"))

    def test_unknown_ratio(self):
        self.assertEqual(core.ratio_text(0.113), "0.11")


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="splitter-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_source(self, w=620, h=5500, name="huge.png"):
        img = Image.new("RGB", (w, h), (240, 240, 240))
        for y in range(0, h, 40):
            for x in range(0, w, 40):
                img.paste((x % 255, y % 255, 128), (x, y, min(w, x + 20), min(h, y + 20)))
        path = os.path.join(self.tmp, name)
        img.save(path)
        return path

    def test_files_are_written_and_readable(self):
        src = self.make_source()
        out = os.path.join(self.tmp, "out")
        written = core.split_file(src, core.Settings(smart=False), out)
        self.assertTrue(written)
        for path in written:
            with Image.open(path) as piece:
                self.assertEqual(piece.width, 620)
                self.assertGreater(piece.height, 100)

    def test_pieces_reassemble_into_the_original(self):
        src = self.make_source(w=300, h=2400)
        out = os.path.join(self.tmp, "out")
        settings = core.Settings(smart=False, overlap=0)
        written = core.split_file(src, settings, out, fmt="png")
        source = Image.open(src).convert("RGB")
        plan = core.plan_pieces(source.width, source.height, settings)
        rebuilt = Image.new("RGB", source.size)
        for piece, path in zip(plan.pieces, written):
            with Image.open(path) as chunk:
                rebuilt.paste(chunk.convert("RGB"), (0, piece.start))
        self.assertEqual(core.pixel_values(rebuilt), core.pixel_values(source))
        source.close()

    def test_jpeg_output_flattens_transparency(self):
        img = Image.new("RGBA", (300, 2000), (255, 0, 0, 0))
        src = os.path.join(self.tmp, "alpha.png")
        img.save(src)
        written = core.split_file(src, core.Settings(smart=False),
                                  os.path.join(self.tmp, "j"), fmt="jpeg", quality=80)
        with Image.open(written[0]) as piece:
            self.assertEqual(piece.mode, "RGB")
            self.assertEqual(piece.getpixel((5, 5)), (255, 255, 255))

    def test_padded_tail_has_the_exact_target_size(self):
        src = self.make_source(w=400, h=3000)
        settings = core.Settings(ratio_w=1, ratio_h=1, mode=core.MODE_EXACT,
                                 tail=core.TAIL_PAD, smart=False)
        written = core.split_file(src, settings, os.path.join(self.tmp, "p"))
        with Image.open(written[-1]) as last:
            self.assertEqual(last.size, (400, 400))

    def test_existing_files_are_not_clobbered(self):
        src = self.make_source(w=300, h=1500)
        out = os.path.join(self.tmp, "twice")
        first = core.split_file(src, core.Settings(smart=False), out)
        second = core.split_file(src, core.Settings(smart=False), out)
        self.assertFalse(set(first) & set(second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
