"""Unit tests for the log reader (no Terminator / display required)."""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from magere_brug import diagnose  # noqa: E402
from magere_brug import log as mblog  # noqa: E402


class TestParseLine(unittest.TestCase):
    def test_parses_logfmt(self):
        line = (
            '2026-08-10T12:00:00.123 WARN pid=4711 mod=placement '
            'event=place.slow duration_ms=312'
        )
        record = diagnose.parse_line(line)
        self.assertEqual(record['level'], 'WARN')
        self.assertEqual(record['pid'], '4711')
        self.assertEqual(record['mod'], 'placement')
        self.assertEqual(record['event'], 'place.slow')
        self.assertEqual(record['duration_ms'], '312')

    def test_parses_quoted_values(self):
        line = '2026-08-10T12:00:00.123 WARN pid=1 mod=t event=e note="two words" n=3'
        record = diagnose.parse_line(line)
        self.assertEqual(record['note'], 'two words')
        self.assertEqual(record['n'], '3')

    def test_parses_json_lines(self):
        line = json.dumps({'event': 'session.start', 'pid': 7, 'level': 'WARN'})
        record = diagnose.parse_line(line)
        self.assertEqual(record['event'], 'session.start')

    def test_ignores_traceback_continuations(self):
        self.assertIsNone(diagnose.parse_line('    | ValueError: boom'))

    def test_ignores_blank_and_garbage(self):
        self.assertIsNone(diagnose.parse_line(''))
        self.assertIsNone(diagnose.parse_line('some unrelated output\n'))

    def test_round_trips_a_real_log_line(self):
        """Parser and formatter must agree — they are edited independently."""
        with tempfile.TemporaryDirectory() as tmp:
            mblog.reset()
            saved = os.environ.pop(mblog.ENV_FORMAT, None)
            try:
                target = os.path.join(tmp, 'mb.log')
                mblog.configure(path=target)
                mblog.get_logger('placement').warn(
                    'place.slow', duration_ms=312, on_parent=True
                )
                with open(target) as handle:
                    record = diagnose.parse_line(handle.readline())
            finally:
                mblog.reset()
                if saved is not None:
                    os.environ[mblog.ENV_FORMAT] = saved
        self.assertEqual(record['event'], 'place.slow')
        self.assertEqual(record['mod'], 'placement')
        self.assertEqual(record['duration_ms'], '312')
        self.assertEqual(record['on_parent'], 'true')


def _record(event, **fields):
    base = {'ts': '2026-08-10T12:00:00.000', 'level': 'WARN', 'pid': '1'}
    base.update(fields)
    base['event'] = event
    return base


class TestSummarise(unittest.TestCase):
    def test_clean_session_pairs_up(self):
        summary = diagnose.summarise(
            [_record(mblog.EVENT_SESSION_START), _record(mblog.EVENT_SESSION_END)]
        )
        self.assertEqual(len(summary['sessions']), 1)
        self.assertTrue(summary['sessions'][0]['clean'])
        self.assertEqual(summary['unclean_sessions'], [])

    def test_missing_shutdown_marker_flags_a_crash(self):
        summary = diagnose.summarise([_record(mblog.EVENT_SESSION_START)])
        self.assertEqual(len(summary['unclean_sessions']), 1)

    def test_restart_under_same_pid_flags_the_earlier_session(self):
        summary = diagnose.summarise(
            [
                _record(mblog.EVENT_SESSION_START),
                _record(mblog.EVENT_SESSION_START),
                _record(mblog.EVENT_SESSION_END),
            ]
        )
        self.assertEqual(len(summary['sessions']), 2)
        self.assertEqual(len(summary['unclean_sessions']), 1)

    def test_sessions_are_tracked_per_pid(self):
        summary = diagnose.summarise(
            [
                _record(mblog.EVENT_SESSION_START, pid='1'),
                _record(mblog.EVENT_SESSION_START, pid='2'),
                _record(mblog.EVENT_SESSION_END, pid='2'),
            ]
        )
        unclean = summary['unclean_sessions']
        self.assertEqual([s['pid'] for s in unclean], ['1'])

    def test_stalls_are_totalled(self):
        summary = diagnose.summarise(
            [
                _record('mainloop.stall', count='1', max_ms='800'),
                _record('mainloop.stall', count='4', max_ms='2500'),
            ]
        )
        self.assertEqual(summary['stalls']['reports'], 2)
        self.assertEqual(summary['stalls']['count'], 5)
        self.assertEqual(summary['stalls']['max_ms'], 2500.0)

    def test_slow_spans_are_grouped_by_event(self):
        summary = diagnose.summarise(
            [
                _record('place.slow', duration_ms='100'),
                _record('place.slow', duration_ms='300'),
                _record('hook.tick.slow', duration_ms='60'),
            ]
        )
        self.assertEqual(summary['slow']['place.slow']['count'], 2)
        self.assertEqual(summary['slow']['place.slow']['max_ms'], 300.0)
        self.assertEqual(summary['slow']['place.slow']['total_ms'], 400.0)
        self.assertIn('hook.tick.slow', summary['slow'])

    def test_errors_are_collected(self):
        summary = diagnose.summarise(
            [_record('boom', level='ERROR'), _record('fine', level='WARN')]
        )
        self.assertEqual(len(summary['errors']), 1)
        self.assertEqual(summary['errors'][0]['event'], 'boom')

    def test_malformed_values_do_not_raise(self):
        summary = diagnose.summarise(
            [_record('mainloop.stall', count='n/a', max_ms='')]
        )
        self.assertEqual(summary['stalls']['reports'], 1)


class TestLogFiles(unittest.TestCase):
    def test_rotated_files_come_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'mb.log')
            for name in (target, target + '.1', target + '.2'):
                with open(name, 'w') as handle:
                    handle.write('x')
            self.assertEqual(
                diagnose.log_files(target),
                [target + '.2', target + '.1', target],
            )

    def test_missing_log_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(diagnose.log_files(os.path.join(tmp, 'absent.log')), [])


class TestCli(unittest.TestCase):
    def test_path_subcommand_prints_the_resolved_path(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = diagnose.main(['--file', '/tmp/x.log', '--path'])
        self.assertEqual(code, 0)
        self.assertEqual(buffer.getvalue().strip(), '/tmp/x.log')

    def test_json_summary_is_valid_json(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = diagnose.main(['--file', '/nonexistent/mb.log', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn('environment', payload)
        self.assertIn('summary', payload)


if __name__ == '__main__':
    unittest.main()
