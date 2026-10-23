"""Make the test suite runnable from a clone, before any install.

`py -3 -m unittest discover -s tests -t .` and `py -3 -m pytest tests` both
work against src/ as-is - nobody has to `pip install -e .` just to run tests.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
