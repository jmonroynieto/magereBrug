"""Diagnostics logging for magereBrug.

Deliberately free of ``gi`` and ``terminatorlib`` imports: this module must be
importable (and testable) outside a running Terminator, which is what lets
``python3 -m magere_brug.diagnose`` and the unit tests work.

Design constraint: the plugin's reported symptom is *freezes*, and this module
runs on the GTK main thread. So the file handler is always attached but the
default level is WARNING — normal operation writes essentially nothing. Turn it
up deliberately:

    MAGEREBRUG_LOG_LEVEL=DEBUG terminator

or in ``~/.config/terminator/config``::

    [plugins]
      [[MagereBrug]]
      debug = True

Line format is logfmt (``key=value``), one event per line, PID on every line so
that concurrent Terminator processes and plugin reloads can be untangled::

    2026-08-10T12:00:00.123 WARN pid=4711 mod=placement event=place.slow duration_ms=312

Set ``MAGEREBRUG_LOG_FORMAT=json`` for JSON Lines instead.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import logging.handlers
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, Iterator, Optional

# ---------------------------------------------------------------- defaults

LOGGER_ROOT = 'magereBrug'

DEFAULT_LEVEL = 'WARNING'
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_BACKUPS = 3

#: A span at or above this many milliseconds is logged at WARNING even when the
#: level would otherwise hide it. 50ms is already a visible hitch at 60Hz.
DEFAULT_SLOW_MS = 50

ENV_LEVEL = 'MAGEREBRUG_LOG_LEVEL'
ENV_FILE = 'MAGEREBRUG_LOG_FILE'
ENV_FORMAT = 'MAGEREBRUG_LOG_FORMAT'
ENV_STDERR = 'MAGEREBRUG_LOG_STDERR'
ENV_MAX_BYTES = 'MAGEREBRUG_LOG_MAX_BYTES'
ENV_SLOW_MS = 'MAGEREBRUG_SLOW_MS'

#: Marker events. ``diagnose`` keys off these to segment the file into sessions
#: and to notice a session that never wrote a shutdown line (i.e. a hard crash).
EVENT_SESSION_START = 'session.start'
EVENT_SESSION_END = 'session.end'

_FIELD_KEY = 'mb_fields'

_state: Dict[str, Any] = {
    'configured': False,
    'path': None,
    'setup_error': None,
    'slow_ms': DEFAULT_SLOW_MS,
}


def _quiet_by_default() -> None:
    """Silence our tree until :func:`configure` runs.

    Without a handler, stdlib logging falls back to ``lastResort``, which
    prints WARNING and above to stderr — the exact behaviour this module
    exists to replace. Propagation is off so we never reach the app's root
    logger either.
    """
    root = logging.getLogger(LOGGER_ROOT)
    root.propagate = False
    if not root.handlers:
        root.addHandler(logging.NullHandler())


_quiet_by_default()


# ------------------------------------------------------------------- paths


def default_log_dir() -> str:
    """XDG state dir for the log, honouring ``XDG_STATE_HOME``."""
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'state'
    )
    return os.path.join(base, 'magereBrug')


def resolve_log_path(explicit: Optional[str] = None) -> str:
    """Log file path: explicit argument, then ``MAGEREBRUG_LOG_FILE``, then XDG."""
    candidate = explicit or os.environ.get(ENV_FILE)
    if candidate:
        return os.path.expanduser(candidate)
    return os.path.join(default_log_dir(), 'magereBrug.log')


def log_path() -> Optional[str]:
    """Path currently being written to, or None if setup failed."""
    return _state.get('path')


def setup_error() -> Optional[str]:
    """Why file logging is unavailable, if it is."""
    return _state.get('setup_error')


# -------------------------------------------------------------- formatting


def _render_value(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        text = '%.1f' % value
    else:
        text = str(value)
    text = text.replace('\n', '\\n').replace('"', "'")
    if not text:
        return '""'
    if any(ch.isspace() for ch in text):
        return '"%s"' % text
    return text


class LogfmtFormatter(logging.Formatter):
    """``ts LEVEL pid=… mod=… event=… key=value …`` — greppable and awk-able."""

    LEVEL_NAMES = {
        logging.DEBUG: 'DEBUG',
        logging.INFO: 'INFO',
        logging.WARNING: 'WARN',
        logging.ERROR: 'ERROR',
        logging.CRITICAL: 'FATAL',
    }

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(record.created))
        stamp = '%s.%03d' % (stamp, int(record.msecs))
        level = self.LEVEL_NAMES.get(record.levelno, record.levelname)
        module = record.name
        if module.startswith(LOGGER_ROOT + '.'):
            module = module[len(LOGGER_ROOT) + 1:]
        parts = [
            stamp,
            level,
            'pid=%d' % record.process,
            'mod=%s' % module,
            'event=%s' % record.getMessage(),
        ]
        for key, value in (getattr(record, _FIELD_KEY, None) or {}).items():
            parts.append('%s=%s' % (key, _render_value(value)))
        line = ' '.join(parts)
        if record.exc_info:
            detail = ''.join(traceback.format_exception(*record.exc_info))
            line += '\n' + '\n'.join(
                '    | ' + row for row in detail.rstrip().splitlines()
            )
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for ingestion into a log pipeline."""

    def format(self, record: logging.LogRecord) -> str:
        module = record.name
        if module.startswith(LOGGER_ROOT + '.'):
            module = module[len(LOGGER_ROOT) + 1:]
        payload: Dict[str, Any] = {
            'ts': time.strftime(
                '%Y-%m-%dT%H:%M:%S', time.localtime(record.created)
            ) + '.%03d' % int(record.msecs),
            'level': record.levelname,
            'pid': record.process,
            'mod': module,
            'event': record.getMessage(),
        }
        payload.update(getattr(record, _FIELD_KEY, None) or {})
        if record.exc_info:
            payload['traceback'] = ''.join(
                traceback.format_exception(*record.exc_info)
            )
        try:
            return json.dumps(payload, default=str, sort_keys=False)
        except Exception:
            return json.dumps({'event': 'log.serialise_failed'})


def make_formatter(style: Optional[str] = None) -> logging.Formatter:
    style = (style or os.environ.get(ENV_FORMAT) or 'logfmt').strip().lower()
    return JsonFormatter() if style == 'json' else LogfmtFormatter()


# ------------------------------------------------------------------- setup


def resolve_level(
    config_level: Optional[str] = None,
    config_debug: Any = None,
) -> int:
    """Level precedence: env, then ``log_level``, then ``debug``, then WARNING.

    Resolved once at startup and cached — never call this from a log site, it
    would drag a Terminator ``Config()`` read onto the main loop.
    """
    raw = os.environ.get(ENV_LEVEL) or config_level
    if not raw and _truthy(config_debug):
        raw = 'DEBUG'
    if not raw:
        raw = DEFAULT_LEVEL
    name = str(raw).strip().upper()
    resolved = logging.getLevelName(name)
    return resolved if isinstance(resolved, int) else logging.WARNING


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _int_env(name: str, fallback: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return fallback


def configure(
    level: Optional[str] = None,
    debug: Any = None,
    path: Optional[str] = None,
    max_bytes: Optional[int] = None,
    backups: int = DEFAULT_BACKUPS,
    style: Optional[str] = None,
    force: bool = False,
) -> Optional[str]:
    """Attach the rotating file handler. Idempotent; never raises.

    Returns the log path, or None if the file handler could not be attached
    (in which case the plugin keeps running and :func:`setup_error` explains).
    """
    if _state['configured'] and not force:
        return _state['path']

    root = logging.getLogger(LOGGER_ROOT)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    root.setLevel(resolve_level(level, debug))
    _state['slow_ms'] = _int_env(ENV_SLOW_MS, DEFAULT_SLOW_MS)
    _state['setup_error'] = None
    _state['configured'] = True

    target = resolve_log_path(path)
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=(
                max_bytes
                if max_bytes is not None
                else _int_env(ENV_MAX_BYTES, DEFAULT_MAX_BYTES)
            ),
            backupCount=backups,
            encoding='utf-8',
            delay=False,
        )
        handler.setFormatter(make_formatter(style))
        root.addHandler(handler)
        _state['path'] = target
    except Exception as exc:
        _state['path'] = None
        _state['setup_error'] = '%s: %s' % (type(exc).__name__, exc)

    if _truthy(os.environ.get(ENV_STDERR)):
        try:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(make_formatter(style))
            root.addHandler(stream)
        except Exception:
            pass

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    return _state['path']


def reset() -> None:
    """Detach handlers and forget setup state (tests use this)."""
    root = logging.getLogger(LOGGER_ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _state.update(
        {
            'configured': False,
            'path': None,
            'setup_error': None,
            'slow_ms': DEFAULT_SLOW_MS,
        }
    )
    _quiet_by_default()


# ------------------------------------------------------------------ clocks


def monotonic_ms() -> float:
    """Monotonic milliseconds — immune to suspend and wall-clock jumps."""
    return time.monotonic() * 1000.0


# ------------------------------------------------------------------ logger


class Log:
    """Thin event-oriented wrapper over a stdlib logger.

    Field rendering is skipped entirely when the level is disabled, so a
    ``log.debug(...)`` on a hot path costs one ``isEnabledFor`` check.
    """

    __slots__ = ('_logger', 'name')

    def __init__(self, name: str) -> None:
        self.name = name
        self._logger = logging.getLogger('%s.%s' % (LOGGER_ROOT, name))

    # -- emit ------------------------------------------------------------

    def _emit(self, level: int, event: str, fields: Dict[str, Any], exc: bool = False) -> None:
        try:
            if not self._logger.isEnabledFor(level):
                return
            self._logger.log(
                level, event, extra={_FIELD_KEY: fields}, exc_info=exc
            )
        except Exception:
            # Logging must never take the plugin down.
            pass

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        """Log the in-flight exception with its traceback."""
        self._emit(logging.ERROR, event, fields, exc=True)

    def enabled(self, level: int = logging.DEBUG) -> bool:
        return self._logger.isEnabledFor(level)

    # -- instruments -----------------------------------------------------

    @contextlib.contextmanager
    def span(
        self, event: str, slow_ms: Optional[float] = None, **fields: Any
    ) -> Iterator[Dict[str, Any]]:
        """Time a block; log it if it is slow (WARNING) or if DEBUG is on.

        This is what attributes a main-loop stall to a specific call. Yields a
        dict the caller can add fields to before the line is written.
        """
        threshold = _state['slow_ms'] if slow_ms is None else slow_ms
        started = monotonic_ms()
        extra: Dict[str, Any] = dict(fields)
        try:
            yield extra
        finally:
            duration = monotonic_ms() - started
            extra['duration_ms'] = round(duration, 1)
            if duration >= threshold:
                self._emit(logging.WARNING, event + '.slow', extra)
            else:
                self._emit(logging.DEBUG, event, extra)

    @contextlib.contextmanager
    def swallow(self, event: str, **fields: Any) -> Iterator[None]:
        """Replace ``except Exception: pass`` where failure is survivable.

        The exception is still suppressed, but it stops being invisible.
        """
        try:
            yield
        except Exception:
            self._emit(logging.DEBUG, event + '.failed', dict(fields), exc=True)

    def guard(self, retval: Any = False, event: Optional[str] = None) -> Callable:
        """Decorate a GLib callback so a raised exception is logged.

        GLib turns an exception in a callback into a ``None`` return, which it
        reads as ``False`` — silently removing the source. That is how a timer
        stops firing with nothing in any log. The wrapper logs the traceback
        and returns *retval*, so a repeating source keeps repeating.
        """

        def decorate(fn: Callable) -> Callable:
            label = event or ('callback.' + getattr(fn, '__name__', 'anon'))

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    self._emit(logging.ERROR, label + '.raised', {}, exc=True)
                    return retval

            return wrapper

        return decorate


_loggers: Dict[str, Log] = {}


def get_logger(name: str) -> Log:
    log = _loggers.get(name)
    if log is None:
        log = Log(name)
        _loggers[name] = log
    return log


# ------------------------------------------------------------ stall detector


class StallDetector:
    """Measures main-loop responsiveness from a fixed-interval heartbeat.

    A GLib timeout that should fire every *interval_ms* but fires late means
    the main loop was blocked in between — which is exactly what a user
    experiences as a freeze. The overshoot is the stall duration.

    The first stall is reported immediately; subsequent ones are aggregated and
    flushed at most once per *summary_ms*, so a persistent stall pattern costs
    one line per window rather than one line per tick.

    The clock is injectable so the arithmetic is unit-testable.
    """

    def __init__(
        self,
        interval_ms: float,
        threshold_ms: float = 200.0,
        summary_ms: float = 30000.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.interval_ms = float(interval_ms)
        self.threshold_ms = float(threshold_ms)
        self.summary_ms = float(summary_ms)
        self._clock = clock or monotonic_ms
        self._last_tick: Optional[float] = None
        self._last_emit: Optional[float] = None
        # Reporting is rate-limited by a quiet period rather than by the stall
        # counter: resetting the counter on every flush would make each new
        # stall look like "the first one" and report immediately, which is how
        # a persistent 1.5s stall pattern turns into a line every 1.5s.
        self._quiet_until: Optional[float] = None
        self._count = 0
        self._max = 0.0
        self._total = 0.0

    def tick(self) -> Optional[Dict[str, Any]]:
        """Record a heartbeat. Returns a report dict when one is due."""
        now = self._clock()
        previous, self._last_tick = self._last_tick, now
        if previous is None:
            self._last_emit = now
            return None

        overshoot = (now - previous) - self.interval_ms
        if overshoot >= self.threshold_ms:
            self._count += 1
            self._max = max(self._max, overshoot)
            self._total += overshoot
        return self._flush(now) if self._due(now) else None

    def _due(self, now: float) -> bool:
        if self._count <= 0:
            return False
        # First stall of a quiet period reports at once; the rest wait.
        return self._quiet_until is None or now >= self._quiet_until

    def _flush(self, now: float) -> Dict[str, Any]:
        window = 0.0 if self._last_emit is None else (now - self._last_emit)
        report = {
            'count': self._count,
            'max_ms': round(self._max, 1),
            'total_ms': round(self._total, 1),
            'window_ms': round(window, 1),
        }
        self._count = 0
        self._max = 0.0
        self._total = 0.0
        self._last_emit = now
        self._quiet_until = now + self.summary_ms
        return report


# -------------------------------------------------------------- session info


def rss_kb() -> Optional[int]:
    """Resident set size in kB from /proc/self/status, or None."""
    try:
        with open('/proc/self/status', 'r') as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def session_fields() -> Dict[str, Any]:
    """Environment facts worth stamping at session start."""
    return {
        'python': '%d.%d.%d' % sys.version_info[:3],
        'session_type': os.environ.get('XDG_SESSION_TYPE') or 'unknown',
        'display': os.environ.get('DISPLAY') or '',
        'wayland': bool(os.environ.get('WAYLAND_DISPLAY')),
        'desktop': os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown',
        'rss_kb': rss_kb(),
    }
