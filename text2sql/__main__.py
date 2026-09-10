"""Entry point so the engine runs as `python -m text2sql "your question"`."""

import sys

from text2sql.cli import main

if __name__ == "__main__":
    sys.exit(main())
