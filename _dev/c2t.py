"""Dev launcher: run the package without installing it.

`py -3 _dev/c2t.py run -f csv` — same argv as the installed `clip2table`.
Kept out of src/ so it never ships.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clip2table.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
