"""Unit tests for the diagnostics logging core (no Terminator / display required)."""

import logging
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from magere_brug import log as mblog  # noqa: E402


class LogTestCase(unittest.TestCase):
    """Isolates each test from the process-wide logging state and environment."""

    ENV_KEYS = (
        mblog.ENV_LEVEL,
        mblog.ENV_FILE,
        mblog.ENV_FORMAT,
        mblog.ENV_STDERR,
        mblog.ENV_MAX_BYTES,
        mblog.ENV_SLOW_MS,
        'XDG_STATE_HOME',
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        mblog.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        mblog.reset()
        self._tmp.cleanup()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def read_log(self, path=None):
        path = path or mblog.log_path()
        with open(path, 'r') as handle:
            return handle.read()


class TestPathResolution(LogTestCase):
    def test_defaults_to_xdg_state_home(self):
        os.environ['XDG_STATE_HOME'] = self.tmpdir
        self.assertEqual(
            mblog.resolve_log_path(),
            os.path.join(self.tmpdir, 'magereBrug', 'magereBrug.log'),
        )

    def test_falls_back_to_local_state(self):
        expected = os.path.join(
            os.path.expanduser('~'), '.local', 'state', 'magereBrug', 'magereBrug.log'
        )
        self.assertEqual(mblog.resolve_log_path(), expected)

    def test_env_override_wins(self):
        os.environ['XDG_STATE_HOME'] = self.tmpdir
        os.environ[mblog.ENV_FILE] = '/somewhere/else.log'
        self.assertEqual(mblog.resolve_log_path(), '/somewhere/else.log')

    def test_explicit_argument_beats_env(self):
        os.environ[mblog.ENV_FILE] = '/somewhere/else.log'
        self.assertEqual(mblog.resolve_log_path('/explicit.log'), '/explicit.log')

    def test_tilde_is_expanded(self):
        os.environ[mblog.ENV_FILE] = '~/mb.log'
        self.assertEqual(
            mblog.resolve_log_path(),
            os.path.join(os.path.expanduser('~'), 'mb.log'),
        )

    def test_configure_creates_file_and_parent_dirs(self):
        target = os.path.join(self.tmpdir, 'nested', 'deeper', 'mb.log')
        path = mblog.configure(path=target)
        self.assertEqual(path, target)
        self.assertTrue(os.path.exists(target))
        self.assertIsNone(mblog.setup_error())

    def test_unwritable_path_degrades_without_raising(self):
        path = mblog.configure(path='/proc/definitely/not/writable/mb.log')
        self.assertIsNone(path)
        self.assertIsNotNone(mblog.setup_error())
        # The plugin must keep working with logging unavailable.
        mblog.get_logger('t').warn('still.alive')


class TestLevelResolution(LogTestCase):
    def test_default_is_warning(self):
        self.assertEqual(mblog.resolve_level(), logging.WARNING)

    def test_config_level_applies(self):
        self.assertEqual(mblog.resolve_level(config_level='debug'), logging.DEBUG)

    def test_debug_flag_implies_debug_level(self):
        self.assertEqual(mblog.resolve_level(config_debug=True), logging.DEBUG)
        self.assertEqual(mblog.resolve_level(config_debug='True'), logging.DEBUG)
        self.assertEqual(mblog.resolve_level(config_debug='no'), logging.WARNING)

    def test_env_beats_config(self):
        os.environ[mblog.ENV_LEVEL] = 'ERROR'
        self.assertEqual(mblog.resolve_level(config_level='DEBUG'), logging.ERROR)

    def test_garbage_level_falls_back_to_warning(self):
        self.assertEqual(mblog.resolve_level(config_level='LOUD'), logging.WARNING)


class TestFiltering(LogTestCase):
    def test_debug_is_suppressed_at_default_level(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        log = mblog.get_logger('filter')
        log.debug('should.not.appear')
        log.warn('should.appear')
        content = self.read_log()
        self.assertNotIn('should.not.appear', content)
        self.assertIn('should.appear', content)

    def test_debug_passes_when_enabled(self):
        mblog.configure(level='DEBUG', path=os.path.join(self.tmpdir, 'mb.log'))
        mblog.get_logger('filter').debug('now.visible')
        self.assertIn('now.visible', self.read_log())


class TestFormatting(LogTestCase):
    def test_logfmt_line_carries_pid_module_and_fields(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        mblog.get_logger('placement').warn('place.slow', duration_ms=312, calls=7)
        line = self.read_log().strip()
        self.assertIn('pid=%d' % os.getpid(), line)
        self.assertIn('mod=placement', line)
        self.assertIn('event=place.slow', line)
        self.assertIn('duration_ms=312', line)
        self.assertIn('calls=7', line)

    def test_values_with_spaces_are_quoted(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        mblog.get_logger('t').warn('an.event', note='two words')
        self.assertIn('note="two words"', self.read_log())

    def test_json_format(self):
        import json

        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'), style='json')
        mblog.get_logger('t').warn('an.event', count=3)
        payload = json.loads(self.read_log().strip())
        self.assertEqual(payload['event'], 'an.event')
        self.assertEqual(payload['count'], 3)
        self.assertEqual(payload['pid'], os.getpid())

    def test_exception_traceback_is_recorded(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        try:
            raise ValueError('boom')
        except ValueError:
            mblog.get_logger('t').exception('it.broke')
        content = self.read_log()
        self.assertIn('event=it.broke', content)
        self.assertIn('ValueError: boom', content)


class TestRotation(LogTestCase):
    def test_rotates_at_size_bound(self):
        target = os.path.join(self.tmpdir, 'mb.log')
        mblog.configure(path=target, max_bytes=2048, backups=2)
        log = mblog.get_logger('rot')
        for index in range(400):
            log.warn('filler.event', index=index, padding='x' * 60)
        self.assertTrue(os.path.exists(target + '.1'), 'expected a rotated file')
        # backupCount is honoured: never more than the configured backups.
        self.assertFalse(os.path.exists(target + '.3'))
        for path in (target, target + '.1'):
            self.assertLessEqual(os.path.getsize(path), 4096)


class TestSpan(LogTestCase):
    def test_slow_span_warns_even_at_default_level(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        with mblog.get_logger('t').span('work', slow_ms=0):
            pass
        content = self.read_log()
        self.assertIn('event=work.slow', content)
        self.assertIn('duration_ms=', content)

    def test_fast_span_is_silent_at_default_level(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        with mblog.get_logger('t').span('work', slow_ms=10000):
            pass
        self.assertEqual(self.read_log().strip(), '')

    def test_caller_can_add_fields(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        with mblog.get_logger('t').span('work', slow_ms=0) as span:
            span['terminals'] = 4
        self.assertIn('terminals=4', self.read_log())

    def test_span_reports_even_when_body_raises(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        with self.assertRaises(RuntimeError):
            with mblog.get_logger('t').span('work', slow_ms=0):
                raise RuntimeError('nope')
        self.assertIn('event=work.slow', self.read_log())


class TestSwallowAndGuard(LogTestCase):
    def test_swallow_suppresses_but_records(self):
        mblog.configure(level='DEBUG', path=os.path.join(self.tmpdir, 'mb.log'))
        with mblog.get_logger('t').swallow('risky.call'):
            raise OSError('denied')
        content = self.read_log()
        self.assertIn('event=risky.call.failed', content)
        self.assertIn('OSError: denied', content)

    def test_guard_returns_retval_on_exception(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))
        log = mblog.get_logger('t')

        @log.guard(retval=True)
        def repeating_timer():
            raise ValueError('boom')

        # True keeps a GLib source alive; a bare exception would return None,
        # which GLib reads as False and silently removes the timer.
        self.assertIs(repeating_timer(), True)
        self.assertIn('raised', self.read_log())

    def test_guard_passes_through_normal_return(self):
        mblog.configure(path=os.path.join(self.tmpdir, 'mb.log'))

        @mblog.get_logger('t').guard(retval=False)
        def fine():
            return 'value'

        self.assertEqual(fine(), 'value')


class FakeClock:
    """Injectable monotonic clock in milliseconds."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


class TestStallDetector(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.detector = mblog.StallDetector(
            interval_ms=500, threshold_ms=200, summary_ms=30000, clock=self.clock
        )

    def tick_after(self, ms):
        self.clock.advance(ms)
        return self.detector.tick()

    def test_first_tick_establishes_baseline(self):
        self.assertIsNone(self.detector.tick())

    def test_on_time_ticks_report_nothing(self):
        self.detector.tick()
        for _ in range(10):
            self.assertIsNone(self.tick_after(500))

    def test_jitter_below_threshold_is_ignored(self):
        self.detector.tick()
        self.assertIsNone(self.tick_after(699))

    def test_overshoot_at_threshold_reports(self):
        self.detector.tick()
        report = self.tick_after(700)
        self.assertIsNotNone(report)
        self.assertEqual(report['count'], 1)
        self.assertAlmostEqual(report['max_ms'], 200.0)

    def test_stall_duration_is_overshoot_not_elapsed(self):
        self.detector.tick()
        report = self.tick_after(2500)
        # 2500ms elapsed on a 500ms heartbeat == 2000ms of blocked main loop.
        self.assertAlmostEqual(report['max_ms'], 2000.0)

    def test_subsequent_stalls_are_aggregated_until_summary_window(self):
        self.detector.tick()
        self.assertIsNotNone(self.tick_after(1500))  # first: immediate
        for _ in range(5):
            self.assertIsNone(self.tick_after(1500))  # aggregated, silent

    def test_summary_flushes_after_window(self):
        self.detector.tick()
        self.tick_after(1500)  # consumes the immediate report
        for _ in range(4):
            self.tick_after(1500)
        report = self.tick_after(30000)
        self.assertIsNotNone(report)
        self.assertEqual(report['count'], 5)
        self.assertGreaterEqual(report['window_ms'], 30000)

    def test_counters_reset_after_flush(self):
        self.detector.tick()
        self.tick_after(1500)
        self.tick_after(30000)
        self.assertIsNone(self.tick_after(500))

    def test_recovery_after_stall_is_silent(self):
        self.detector.tick()
        self.tick_after(5000)
        for _ in range(10):
            self.assertIsNone(self.tick_after(500))


class TestSessionFields(unittest.TestCase):
    def test_session_fields_are_serialisable(self):
        fields = mblog.session_fields()
        for key in ('python', 'session_type', 'desktop'):
            self.assertIn(key, fields)

    def test_rss_kb_is_positive_on_linux(self):
        value = mblog.rss_kb()
        if value is not None:  # non-Linux would return None
            self.assertGreater(value, 0)


if __name__ == '__main__':
    unittest.main()
