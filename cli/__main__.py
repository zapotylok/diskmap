"""python -m cli <command> — see cli.main."""

import sys

from cli.main import main

sys.exit(main(sys.argv[1:]))
