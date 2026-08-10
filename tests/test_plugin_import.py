"""Import ``magere_brug.plugin`` against stubbed GTK / Terminator.

The failure this guards against is specific and nasty: Terminator loads plugins
inside a broad try/except, so a NameError or bad import in ``plugin.py`` makes
the plugin quietly not exist — no dialog, no message, nothing in any log. These
tests give that failure a name at test time instead.

They cannot exercise GTK behaviour, only import-time and wiring correctness.
"""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STUBBED = (
    'gi',
    'gi.repository',
    'terminatorlib',
    'terminatorlib.plugin',
    'terminatorlib.terminator',
    'terminatorlib.util',
    'terminatorlib.config',
)


def install_stubs():
    """Register fake gi / terminatorlib modules; returns the saved originals."""
    saved = {name: sys.modules.get(name) for name in STUBBED}
    saved['magere_brug.plugin'] = sys.modules.get('magere_brug.plugin')
    saved['magere_brug.placement'] = sys.modules.get('magere_brug.placement')

    gi = types.ModuleType('gi')
    gi.require_version = lambda *a, **k: None
    repository = types.ModuleType('gi.repository')
    for name in ('Gtk', 'Gdk', 'GLib', 'Pango'):
        setattr(repository, name, mock.MagicMock(name=name))
    gi.repository = repository

    terminatorlib = types.ModuleType('terminatorlib')

    tl_plugin = types.ModuleType('terminatorlib.plugin')

    class MenuItem(object):
        def __init__(self):
            pass

    tl_plugin.MenuItem = MenuItem

    tl_terminator = types.ModuleType('terminatorlib.terminator')
    tl_terminator.Terminator = mock.MagicMock(name='Terminator')

    tl_util = types.ModuleType('terminatorlib.util')
    tl_util.dbg = lambda *a, **k: None
    tl_util.err = lambda *a, **k: None

    tl_config = types.ModuleType('terminatorlib.config')
    tl_config.Config = mock.MagicMock(name='Config')

    sys.modules.update(
        {
            'gi': gi,
            'gi.repository': repository,
            'terminatorlib': terminatorlib,
            'terminatorlib.plugin': tl_plugin,
            'terminatorlib.terminator': tl_terminator,
            'terminatorlib.util': tl_util,
            'terminatorlib.config': tl_config,
        }
    )
    # Force a fresh import against the stubs.
    sys.modules.pop('magere_brug.plugin', None)
    sys.modules.pop('magere_brug.placement', None)
    return saved


def restore_stubs(saved):
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class TestPluginImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = install_stubs()
        from magere_brug.plugin import MagereBrug

        cls.MagereBrug = MagereBrug

    @classmethod
    def tearDownClass(cls):
        restore_stubs(cls._saved)

    def test_entry_point_exposes_the_class(self):
        import magereBrug

        self.assertEqual(magereBrug.AVAILABLE, ['MagereBrug'])

    def test_declares_terminal_menu_capability(self):
        self.assertEqual(self.MagereBrug.capabilities, ['terminal_menu'])

    def test_diagnostics_surface_exists(self):
        """The context-menu route to the log path must not silently disappear."""
        self.assertTrue(hasattr(self.MagereBrug, '_show_diagnostics'))

    def test_instrumentation_hooks_exist(self):
        for name in (
            '_on_watchdog_tick',
            '_start_logging',
            '_on_window_add',
            '_gather_terminals_inner',
        ):
            self.assertTrue(hasattr(self.MagereBrug, name), name)

    def test_placement_module_imports(self):
        from magere_brug.placement import PlacementController

        controller = PlacementController()
        # Re-entrancy guard must exist and start disengaged.
        self.assertFalse(controller._placing)
        self.assertEqual(controller._place_calls, 0)

    def test_place_is_a_noop_without_a_window(self):
        from magere_brug.placement import PlacementController

        self.assertFalse(PlacementController().place(None, None))


if __name__ == '__main__':
    unittest.main()
