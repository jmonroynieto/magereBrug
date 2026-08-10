"""Log reader and environment report for magereBrug.

    python3 -m magere_brug.diagnose            # summary
    python3 -m magere_brug.diagnose --tail 50  # recent lines
    python3 -m magere_brug.diagnose --follow   # live tail
    python3 -m magere_brug.diagnose --json     # machine-readable summary

Like :mod:`magere_brug.log`, this module imports no ``gi`` and no
``terminatorlib`` at import time, so it runs from any Python on the box while
Terminator is running (or after it has crashed).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

from magere_brug.log import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    resolve_log_path,
)

_LEVELS = ('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')


# ------------------------------------------------------------------ parsing


def _split_logfmt(text: str) -> Dict[str, str]:
    """Parse ``key=value`` pairs, respecting double-quoted values."""
    fields: Dict[str, str] = {}
    token: List[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            continue
        if char == ' ' and not quoted:
            if token:
                _store_pair(fields, ''.join(token))
                token = []
            continue
        token.append(char)
    if token:
        _store_pair(fields, ''.join(token))
    return fields


def _store_pair(fields: Dict[str, str], token: str) -> None:
    if '=' in token:
        key, _, value = token.partition('=')
        fields[key] = value


def parse_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one logfmt or JSON log line into a dict, or None if unparseable.

    Continuation lines (indented traceback rows) and blanks return None.
    """
    line = line.rstrip('\n')
    if not line.strip() or line.startswith('    |'):
        return None
    stripped = line.lstrip()
    if stripped.startswith('{'):
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    parts = line.split(' ', 2)
    if len(parts) < 3 or parts[1] not in _LEVELS:
        return None
    record: Dict[str, Any] = {'ts': parts[0], 'level': parts[1]}
    record.update(_split_logfmt(parts[2]))
    return record


def read_records(path: str) -> Iterator[Dict[str, Any]]:
    """Yield parsed records from the log and its rotated siblings, oldest first."""
    for candidate in log_files(path):
        try:
            with open(candidate, 'r', errors='replace') as handle:
                for line in handle:
                    record = parse_line(line)
                    if record is not None:
                        yield record
        except OSError:
            continue


def log_files(path: str) -> List[str]:
    """The active log plus existing rotated backups, oldest first."""
    rotated = []
    index = 1
    while True:
        candidate = '%s.%d' % (path, index)
        if not os.path.exists(candidate):
            break
        rotated.append(candidate)
        index += 1
    return list(reversed(rotated)) + ([path] if os.path.exists(path) else [])


# ---------------------------------------------------------------- analysis


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def summarise(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold the log into sessions, stalls, slow spans and errors."""
    sessions: List[Dict[str, Any]] = []
    open_sessions: Dict[str, Dict[str, Any]] = {}
    stalls: Dict[str, Any] = {'reports': 0, 'count': 0, 'max_ms': 0.0}
    slow: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, Any]] = []
    total = 0

    for record in records:
        total += 1
        event = str(record.get('event', ''))
        pid = str(record.get('pid', '?'))

        if event == EVENT_SESSION_START:
            session = {
                'pid': pid,
                'start': record.get('ts'),
                'end': None,
                'clean': False,
                'version': record.get('version'),
                'terminator': record.get('terminator'),
                'session_type': record.get('session_type'),
            }
            # A restart under a recycled PID means the previous one never
            # wrote a shutdown line; it stays in `sessions` marked unclean.
            open_sessions.pop(pid, None)
            open_sessions[pid] = session
            sessions.append(session)
        elif event == EVENT_SESSION_END:
            session = open_sessions.pop(pid, None)
            if session is not None:
                session['end'] = record.get('ts')
                session['clean'] = True

        if event == 'mainloop.stall':
            stalls['reports'] += 1
            stalls['count'] += int(_as_float(record.get('count'), 1))
            stalls['max_ms'] = max(stalls['max_ms'], _as_float(record.get('max_ms')))
        elif event.endswith('.slow'):
            entry = slow.setdefault(
                event, {'count': 0, 'max_ms': 0.0, 'total_ms': 0.0}
            )
            duration = _as_float(record.get('duration_ms'))
            entry['count'] += 1
            entry['max_ms'] = max(entry['max_ms'], duration)
            entry['total_ms'] += duration

        if record.get('level') in ('ERROR', 'FATAL'):
            errors.append(
                {
                    'ts': record.get('ts'),
                    'pid': pid,
                    'mod': record.get('mod'),
                    'event': event,
                }
            )

    return {
        'records': total,
        'sessions': sessions,
        'unclean_sessions': [s for s in sessions if not s['clean']],
        'stalls': stalls,
        'slow': slow,
        'errors': errors,
    }


# -------------------------------------------------------------- environment


def _terminator_version() -> str:
    try:
        result = subprocess.run(
            ['terminator', '--version'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = (result.stdout or result.stderr or '').strip()
        if output:
            return output.splitlines()[0]
    except Exception:
        pass
    return 'unknown'


def _gtk_version() -> str:
    """Best-effort GTK version. Imported lazily so this module stays gi-free."""
    try:
        import gi  # noqa: PLC0415 - deliberately lazy

        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk

        return '%d.%d.%d' % (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Gtk.get_micro_version(),
        )
    except Exception:
        return 'unavailable'


def environment() -> Dict[str, Any]:
    try:
        from magere_brug import __version__
    except Exception:
        __version__ = 'unknown'
    return {
        'plugin_version': __version__,
        'python': '%d.%d.%d' % sys.version_info[:3],
        'terminator': _terminator_version(),
        'gtk': _gtk_version(),
        'session_type': os.environ.get('XDG_SESSION_TYPE') or 'unknown',
        'display': os.environ.get('DISPLAY') or '',
        'wayland_display': os.environ.get('WAYLAND_DISPLAY') or '',
        'desktop': os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown',
    }


# ------------------------------------------------------------------ output


def _tail(path: str, count: int) -> List[str]:
    try:
        with open(path, 'r', errors='replace') as handle:
            return handle.readlines()[-count:]
    except OSError as exc:
        return ['<cannot read %s: %s>\n' % (path, exc)]


def _follow(path: str) -> int:
    """Stream new lines as they are written, surviving rotation."""
    print('following %s (Ctrl+C to stop)' % path)
    handle = None
    inode = None
    try:
        while True:
            try:
                stat = os.stat(path)
            except OSError:
                time.sleep(0.5)
                continue
            if handle is None or inode != stat.st_ino:
                if handle is not None:
                    handle.close()
                handle = open(path, 'r', errors='replace')
                handle.seek(0, os.SEEK_END)
                inode = stat.st_ino
            line = handle.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        if handle is not None:
            handle.close()


def _print_report(path: str, summary: Dict[str, Any], env: Dict[str, Any]) -> None:
    print('magereBrug diagnostics')
    print('=' * 60)
    print('log file : %s' % path)
    if os.path.exists(path):
        stat = os.stat(path)
        print(
            'size     : %.1f KiB, modified %s'
            % (
                stat.st_size / 1024.0,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
            )
        )
        others = [p for p in log_files(path) if p != path]
        if others:
            print('rotated  : %s' % ', '.join(os.path.basename(p) for p in others))
    else:
        print('size     : (not created yet — plugin has not logged)')
    print()

    print('environment')
    print('-' * 60)
    for key, value in env.items():
        print('  %-16s %s' % (key, value))
    print()

    sessions = summary['sessions']
    print('sessions (%d parsed records)' % summary['records'])
    print('-' * 60)
    if not sessions:
        print('  none recorded')
    for session in sessions[-10:]:
        status = 'clean' if session['clean'] else 'NO SHUTDOWN MARKER'
        print(
            '  pid=%-7s %s → %s  [%s]'
            % (
                session['pid'],
                session['start'],
                session['end'] or '-',
                status,
            )
        )
    unclean = summary['unclean_sessions']
    if unclean:
        print()
        print(
            '  %d session(s) ended without a shutdown marker. That is the'
            % len(unclean)
        )
        print('  signature of a hard crash (or a kill), not a normal exit.')
        print('  A GTK/VTE segfault leaves nothing else in this log.')
    print()

    stalls = summary['stalls']
    print('main-loop stalls')
    print('-' * 60)
    if stalls['reports'] == 0:
        print('  none recorded — the main loop stayed responsive, or the')
        print('  plugin has not run since the watchdog was added.')
        print('  (stalls log at WARNING, so the default level catches them)')
    else:
        print('  reports=%d  stalls=%d  worst=%.0fms'
              % (stalls['reports'], stalls['count'], stalls['max_ms']))
    print()

    print('slow operations')
    print('-' * 60)
    if not summary['slow']:
        print('  none recorded')
    for event, entry in sorted(
        summary['slow'].items(), key=lambda kv: -kv[1]['total_ms']
    ):
        print(
            '  %-32s count=%-5d max=%.0fms total=%.0fms'
            % (event, entry['count'], entry['max_ms'], entry['total_ms'])
        )
    print()

    errors = summary['errors']
    print('errors (%d)' % len(errors))
    print('-' * 60)
    if not errors:
        print('  none recorded')
    for entry in errors[-10:]:
        print('  %s pid=%s %s %s'
              % (entry['ts'], entry['pid'], entry['mod'], entry['event']))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python3 -m magere_brug.diagnose',
        description='Inspect magereBrug logs and report the runtime environment.',
    )
    parser.add_argument('--file', help='log file to read (default: resolved path)')
    parser.add_argument('--tail', type=int, metavar='N', help='print last N lines')
    parser.add_argument('--follow', action='store_true', help='stream new lines')
    parser.add_argument('--json', action='store_true', help='machine-readable summary')
    parser.add_argument('--path', action='store_true', help='print log path and exit')
    args = parser.parse_args(argv)

    path = resolve_log_path(args.file)

    if args.path:
        print(path)
        return 0
    if args.follow:
        return _follow(path)
    if args.tail:
        sys.stdout.writelines(_tail(path, args.tail))
        return 0

    summary = summarise(read_records(path))
    env = environment()
    if args.json:
        print(json.dumps({'log_file': path, 'environment': env, 'summary': summary},
                         indent=2, default=str))
        return 0

    _print_report(path, summary, env)
    return 0


if __name__ == '__main__':
    sys.exit(main())
