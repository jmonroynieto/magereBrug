"""Pure helpers (no Terminator / GTK runtime required for most of these)."""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from magere_brug.constants import WORD_BOUNDARY


def shorten_path(path: str) -> str:
    """Replace $HOME prefix with ~."""
    try:
        home = os.path.expanduser('~')
        if path == home:
            return '~'
        prefix = home + os.sep
        if path.startswith(prefix):
            return '~' + path[len(home):]
    except Exception:
        pass
    return path


def cmdline_for_pid(pid: Any, max_len: int = 60) -> Optional[str]:
    """Read a concise command line from /proc/<pid>/cmdline."""
    try:
        with open('/proc/%s/cmdline' % int(pid), 'rb') as fh:
            raw = fh.read()
        if not raw:
            return None
        parts = [p.decode('utf-8', 'replace') for p in raw.split(b'\x00') if p]
        if not parts:
            return None
        base = os.path.basename(parts[0])
        if base in ('bash', 'zsh', 'fish', 'sh', 'dash', 'tmux', 'screen') and len(parts) == 1:
            return base
        text = ' '.join([base] + parts[1:3])
        if len(text) > max_len:
            return text[: max_len - 3] + '...'
        return text
    except Exception:
        return None


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Subsequence fuzzy score; higher is better. None means no match.

    Prefers early, contiguous, and word-boundary matches.
    """
    if not query:
        return 0
    text_l = text.lower()
    query_l = query.lower()

    pos = text_l.find(query_l)
    if pos >= 0:
        score = 1000 - pos
        if pos == 0 or (pos > 0 and text_l[pos - 1] in WORD_BOUNDARY):
            score += 80
        return score

    ti = 0
    first = last = None
    gaps = 0
    for qc in query_l:
        found = text_l.find(qc, ti)
        if found < 0:
            return None
        if first is None:
            first = found
        else:
            gaps += found - ti
        last = found
        ti = found + 1

    length = (last - first + 1) if first is not None else 1
    score = 400 - first - gaps - (length - len(query_l))
    if first == 0 or (first > 0 and text_l[first - 1] in WORD_BOUNDARY):
        score += 40
    return score


def filter_and_group(
    items: Sequence[Dict[str, Any]],
    query: str,
) -> 'OrderedDict[Any, Dict[str, Any]]':
    """Score, sort, and group terminal rows by window.

    Each item needs: search, mru_index, win_key, win_title.
    Returns OrderedDict: win_key -> {'title': str, 'items': [item, ...]}.
    """
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for item in items:
        score = fuzzy_score(query, item.get('search', ''))
        if score is None:
            continue
        scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1].get('mru_index', 10 ** 6)))

    groups: 'OrderedDict[Any, Dict[str, Any]]' = OrderedDict()
    for _score, item in scored:
        key = item.get('win_key', 0)
        if key not in groups:
            groups[key] = {'title': item.get('win_title', 'Window'), 'items': []}
        groups[key]['items'].append(item)

    for group in groups.values():
        group['items'].sort(key=lambda it: it.get('mru_index', 10 ** 6))
    return groups


def first_attr(obj: Any, names: Iterable[str]) -> Any:
    """Return the first truthy attribute/call result from *names*, or None."""
    for name in names:
        if not hasattr(obj, name):
            continue
        try:
            val = getattr(obj, name)
            result = val() if callable(val) else val
            if result:
                return result
        except Exception:
            continue
    return None


def safe_call(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default
