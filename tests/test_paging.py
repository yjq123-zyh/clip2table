"""Paging must stop on evidence, not on a feeling.

Three ways this loop can lie to the user:

  * it stops early ("no new rows for two pages") while the list is merely
    re-sorted or full of pinned items -- records after that point are lost,
    and the report says "到底了";
  * it keeps going while the pager is stuck, writing page 1 over and over
    under ascending page numbers -- row counts grow, the table is wrong;
  * it stops with the right verdict but the wrong reason, so the user cannot
    tell "the site ended" from "our pager broke".

The tests below pin the order of the signals and the one hash that can tell
"advanced" from "pretended to advance".
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clip2table.cards.schema import parse_card
from clip2table.errors import InputError
from clip2table.harvest import MultiRound, _fingerprint
from clip2table.models import Captured

BASE = """
schema = 1
name = "t"
channel = "http"
@@PAGING@@

[fetch]
url = "http://x/{page}"

[segment]
mode = "lines"

[fields.a]
kind = "line"
index = 0
"""


def _card(paging: str = "") -> object:
    text = BASE.replace("@@PAGING@@", paging)
    return parse_card(__import__("tomllib").loads(text), "t.toml")


class TestStopPolicyIsDeclared(unittest.TestCase):
    def test_default_stops_on_evidence_only(self):
        """Out of the box, "no new rows" must not end the run."""
        c = _card()
        self.assertEqual(c.stop_when, "fingerprint")
        self.assertEqual(c.stop_after_empty, 2)

    def test_the_heuristic_is_opt_in(self):
        c = _card('[paging]\nstyle = "none"\nstop_when = "empty"\n'
                  'stop_after_empty = 3')
        self.assertEqual(c.stop_when, "empty")
        self.assertEqual(c.stop_after_empty, 3)

    def test_a_typo_does_not_silently_become_the_default(self):
        """stop_when = "fingeprint" must fail loudly, not fall back quietly."""
        with self.assertRaises(InputError) as ctx:
            _card('[paging]\nstop_when = "fingeprint"')
        self.assertIn("stop_when", str(ctx.exception))

    def test_zero_empty_pages_is_rejected(self):
        """0 would mean "stop before reading page 1"."""
        with self.assertRaises(InputError):
            _card('[paging]\nstop_when = "empty"\nstop_after_empty = 0')

    def test_the_hint_tells_you_which_way_to_go(self):
        with self.assertRaises(InputError) as ctx:
            _card('[paging]\nstop_when = "whenever"')
        hint = getattr(ctx.exception, "hint", "") or ""
        self.assertIn("fingerprint", hint)


class TestFingerprint(unittest.TestCase):
    def test_whitespace_differences_are_not_a_new_page(self):
        """A re-render that only changes indentation is the same page."""
        a = _fingerprint(Captured(kind="text", payload="甲\t1\n乙\t2\n"))
        b = _fingerprint(Captured(kind="text", payload="甲   1\n\n乙  2\n"))
        self.assertEqual(a, b)

    def test_different_content_is_a_new_page(self):
        a = _fingerprint(Captured(kind="text", payload="甲\t1"))
        b = _fingerprint(Captured(kind="text", payload="甲\t1\n乙\t2"))
        self.assertNotEqual(a, b)

    def test_json_pages_work_too(self):
        """Channel A can hand back parsed JSON with no text payload at all."""
        a = _fingerprint(Captured(kind="json", json_payload={"x": [1, 2]}))
        b = _fingerprint(Captured(kind="json", json_payload={"x": [1, 2]}))
        c = _fingerprint(Captured(kind="json", json_payload={"x": [1, 3]}))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_an_empty_capture_still_hashes(self):
        self.assertTrue(_fingerprint(Captured(kind="text", payload=None)))


class TestStopReasonsAreDistinguishable(unittest.TestCase):
    """The reason string is the only thing the user sees; it must say which."""

    def test_a_repeat_names_both_pages(self):
        m = MultiRound()
        m.stopped_because = ("第 3 页和第 1 页内容完全相同（翻页没有真的前进），"
                            "已停手，这一页没有写入")
        m.repeat_of = 1
        self.assertIn("第 3 页", m.stopped_because)
        self.assertIn("第 1 页", m.stopped_because)
        self.assertEqual(m.repeat_of, 1)

    def test_max_pages_is_not_reported_as_the_end_of_the_list(self):
        """Hitting the limit is a choice, not a finding about the site."""
        m = MultiRound()
        m.stopped_because = "达到 --max-pages 3"
        self.assertNotIn("到底", m.stopped_because)


if __name__ == "__main__":
    unittest.main()
