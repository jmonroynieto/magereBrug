"""Unit tests for pure helpers (no Terminator / display required)."""

import os
import sys
import tempfile
import unittest
from collections import OrderedDict

# Repo root (plugins dir) on path when running: python -m unittest discover
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from magere_brug.util import (  # noqa: E402
    cmdline_for_pid,
    filter_and_group,
    first_attr,
    fuzzy_score,
    sanitize_title,
    shorten_path,
)


class TestShortenPath(unittest.TestCase):
    def test_home(self):
        home = os.path.expanduser('~')
        self.assertEqual(shorten_path(home), '~')

    def test_under_home(self):
        home = os.path.expanduser('~')
        self.assertEqual(shorten_path(home + '/code/app'), '~/code/app')

    def test_other(self):
        self.assertEqual(shorten_path('/var/log'), '/var/log')


class TestFuzzyScore(unittest.TestCase):
    def test_empty_query(self):
        self.assertEqual(fuzzy_score('', 'anything'), 0)

    def test_no_match(self):
        self.assertIsNone(fuzzy_score('xyz', 'abc'))

    def test_substring_beats_scattered(self):
        sub = fuzzy_score('proj', 'my-project')
        scat = fuzzy_score('prj', 'p-x-r-x-j')
        self.assertIsNotNone(sub)
        self.assertIsNotNone(scat)
        self.assertGreater(sub, scat)

    def test_boundary_bonus(self):
        at_start = fuzzy_score('bash', 'bash')
        mid = fuzzy_score('bash', 'xbash')
        self.assertGreater(at_start, mid)


class TestFilterAndGroup(unittest.TestCase):
    def test_groups_by_window_mru_within(self):
        items = [
            {
                'search': 'alpha shell',
                'mru_index': 1,
                'win_key': 1,
                'win_title': 'Win A',
                'title': 'alpha',
            },
            {
                'search': 'beta shell',
                'mru_index': 0,
                'win_key': 1,
                'win_title': 'Win A',
                'title': 'beta',
            },
            {
                'search': 'gamma other',
                'mru_index': 2,
                'win_key': 2,
                'win_title': 'Win B',
                'title': 'gamma',
            },
        ]
        groups = filter_and_group(items, '')
        self.assertIsInstance(groups, OrderedDict)
        self.assertEqual(list(groups.keys()), [1, 2])
        # Within Win A, MRU index 0 first
        self.assertEqual(
            [i['title'] for i in groups[1]['items']],
            ['beta', 'alpha'],
        )

    def test_filter_drops_non_matches(self):
        items = [
            {
                'search': 'npm run dev',
                'mru_index': 0,
                'win_key': 1,
                'win_title': 'W',
            },
            {
                'search': 'ssh host',
                'mru_index': 1,
                'win_key': 1,
                'win_title': 'W',
            },
        ]
        groups = filter_and_group(items, 'npm')
        flat = [i for g in groups.values() for i in g['items']]
        self.assertEqual(len(flat), 1)
        self.assertIn('npm', flat[0]['search'])


class TestFirstAttr(unittest.TestCase):
    def test_callable(self):
        class O:
            def get_x(self):
                return 'hi'

        self.assertEqual(first_attr(O(), ('get_x',)), 'hi')

    def test_missing(self):
        self.assertIsNone(first_attr(object(), ('nope',)))


class TestSanitizeTitle(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(sanitize_title('a\nb\tc'), 'abc')

    def test_strips_whitespace_ends(self):
        self.assertEqual(sanitize_title('  hello  '), 'hello')

    def test_truncates_with_ellipsis(self):
        text = 'x' * 100
        result = sanitize_title(text, max_len=10)
        self.assertEqual(result, 'xxxxxxx...')
        self.assertEqual(len(result), 10)

    def test_short_text_unchanged(self):
        self.assertEqual(sanitize_title('short', max_len=60), 'short')


class TestCmdline(unittest.TestCase):
    def test_missing_pid(self):
        self.assertIsNone(cmdline_for_pid(999999999))

    def test_self_pid(self):
        # Current process should have a readable cmdline.
        text = cmdline_for_pid(os.getpid())
        # May be None on exotic systems; if present, non-empty string.
        if text is not None:
            self.assertIsInstance(text, str)
            self.assertTrue(len(text) > 0)


if __name__ == '__main__':
    unittest.main()
