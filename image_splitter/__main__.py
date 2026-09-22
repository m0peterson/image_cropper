# -*- coding: utf-8 -*-
"""`python -m image_splitter` -- GUI with no arguments, CLI with them."""

import sys

from . import cli, gui


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return gui.main()
    if argv[0] in ("--gui", "-g"):
        return gui.main(argv[1:])
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
