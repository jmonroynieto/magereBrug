"""Terminator plugin entry point for magereBrug.

Terminator loads every ``*.py`` in the plugins directory that defines
``AVAILABLE``. Keep this file thin; implementation lives in ``magere_brug/``.
"""

from magere_brug.plugin import MagereBrug

# Every plugin class you want Terminator to load *must* be listed here.
AVAILABLE = ['MagereBrug']

__all__ = ['AVAILABLE', 'MagereBrug']
