# -*- coding: utf-8 -*-
"""Regenerate assets/icon.ico and assets/icon.png. Run it only when the look changes."""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 256
BG = (33, 36, 41)
SHEET = (244, 243, 240)
INK = (108, 116, 128)
CUT = (219, 62, 48)


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, SIZE - 6, SIZE - 6), radius=46, fill=BG)
    sheet = (86, 22, 170, SIZE - 22)
    d.rectangle(sheet, fill=SHEET)
    for y in range(40, SIZE - 40, 16):
        width = 58 if (y // 16) % 3 else 40
        d.rectangle((98, y, 98 + width, y + 6), fill=INK)
    for y in (96, 168):                       # the cuts
        for x in range(74, 184, 16):
            d.rectangle((x, y - 3, x + 9, y + 3), fill=CUT)
    return img


def main() -> None:
    img = build()
    img.save(os.path.join(HERE, "icon.png"))
    img.save(os.path.join(HERE, "icon.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
                    (256, 256)])
    print("written icon.png / icon.ico")


if __name__ == "__main__":
    main()
