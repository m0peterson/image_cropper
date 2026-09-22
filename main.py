# -*- coding: utf-8 -*-
"""Entry point for the frozen .exe and for `python main.py`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from image_splitter.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
