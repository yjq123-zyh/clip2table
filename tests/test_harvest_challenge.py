"""A challenge wall must stop the harvest before a single key is sent.

The predicate is measured against a local site that reproduces the vendor
markup (`_probe/challenge_site.py`): 4 walls refused, 5 controls passed. These
tests pin the separation so a later "let's be more careful" change cannot start
refusing normal pages - a false positive here means the tool stops working on a
site the user can see perfectly well.
"""
from __future__ import annotations

import json
import unittest

from clip2table.errors import ChannelError
from clip2table.harvest import _check_challenge


class FakeCDP:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def eval(self, expr: str):
        self.calls.append(expr)
        return self.payload


def _wall(*walls: str) -> str:
    return json.dumps({"walls": list(walls), "title": "Just a moment..."})


class TestChallengeWallRefused(unittest.TestCase):
    def test_cloudflare_interstitial(self):
        cl = FakeCDP(_wall("marker:#challenge-form"))
        with self.assertRaises(ChannelError) as ctx:
            _check_challenge(cl, [])
        self.assertIn("人机验证墙", str(ctx.exception))
        self.assertIn("一个键都没发", str(ctx.exception))
        self.assertIn("challenge-form", str(ctx.exception))

    def test_iframe_captcha(self):
        cl = FakeCDP(_wall("iframe:newassets.hcaptcha.com"))
        with self.assertRaises(ChannelError):
            _check_challenge(cl, [])

    def test_geetest_panel(self):
        cl = FakeCDP(_wall("panel:.geetest_panel"))
        with self.assertRaises(ChannelError):
            _check_challenge(cl, [])

    def test_the_hint_says_solve_it_and_that_we_will_not_retry(self):
        cl = FakeCDP(_wall("marker:#challenge-form"))
        with self.assertRaises(ChannelError) as ctx:
            _check_challenge(cl, [])
        hint = ctx.exception.hint
        self.assertIn("手动完成验证", hint)
        self.assertIn("不会自动重试", hint)

    def test_a_clean_page_passes(self):
        cl = FakeCDP(_wall())
        notes: list[str] = []
        _check_challenge(cl, notes)
        self.assertEqual(notes, [])

    def test_unreadable_probe_does_not_stop_a_harvest(self):
        # The guard exists to catch one specific page shape. If the probe itself
        # breaks (older browser, CSP, eval error), continuing is right: a broken
        # detector must not become a broken tool.
        for payload in (None, "", "not json", "{}", '{"walls": null}'):
            with self.subTest(payload=payload):
                _check_challenge(FakeCDP(payload), [])

    def test_probe_error_does_not_stop_a_harvest(self):
        class Boom(FakeCDP):
            def eval(self, expr):
                raise RuntimeError("CDP went away")

        _check_challenge(Boom(None), [])


if __name__ == "__main__":
    unittest.main()
