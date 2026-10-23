"""`python -m clip2table` - the path that works before install.

The console script is the normal way in, but a cloned repo has no console
script yet, and `python -m clip2table` failing with "No module named
clip2table.__main__" is a bad first minute.
"""
from __future__ import annotations

from .cli import main

raise SystemExit(main())
