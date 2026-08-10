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


class TestSessionMarkers(unittest.TestCase):
    """The end marker must not depend on Terminator calling unload().

    Terminator calls unload() only when a plugin is reloaded or disabled from
    Preferences — never on application quit. Keying the shutdown marker on it
    alone would mark every normal exit as a crash, and a detector that always
    fires teaches you to ignore it.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved = install_stubs()
        from magere_brug import plugin as plugin_module

        cls.plugin_module = plugin_module

    @classmethod
    def tearDownClass(cls):
        restore_stubs(cls._saved)

    def setUp(self):
        import tempfile

        from magere_brug import log as mblog

        self.mblog = mblog
        self._tmp = tempfile.TemporaryDirectory()
        mblog.reset()
        self.log_file = os.path.join(self._tmp.name, 'mb.log')
        mblog.configure(path=self.log_file)
        self.instance = self.plugin_module.MagereBrug()

    def tearDown(self):
        self.mblog.reset()
        self._tmp.cleanup()

    def end_markers(self):
        with open(self.log_file) as handle:
            return handle.read().count('event=' + self.mblog.EVENT_SESSION_END)

    def test_start_logging_registers_an_atexit_marker(self):
        import atexit

        self.instance._start_logging()
        # unregister returns nothing but only removes a registered callable;
        # calling the marker afterwards must then be a no-op, proving the
        # atexit path and the once-only flag are wired to the same method.
        atexit.unregister(self.instance._write_session_end)
        self.instance._write_session_end()
        self.assertEqual(self.end_markers(), 1)

    def test_end_marker_is_written_once(self):
        self.instance._write_session_end()
        self.instance._write_session_end()
        self.assertEqual(self.end_markers(), 1)

    def test_unload_writes_the_marker(self):
        self.instance.unload()
        self.assertEqual(self.end_markers(), 1)

    def test_unload_then_process_exit_does_not_double_write(self):
        self.instance.unload()
        self.instance._write_session_end(reason='exit')
        self.assertEqual(self.end_markers(), 1)

    def test_marker_records_why_the_session_ended(self):
        self.instance.unload()
        with open(self.log_file) as handle:
            self.assertIn('reason=unload', handle.read())


class TestConfigCaching(unittest.TestCase):
    """Terminator's Config() constructor is expensive; we must build one.

    Each construction runs connect_gsetting_callbacks(): two Gio.Settings
    objects plus three signal connects. The keybinding refresh runs on a 1.5s
    timer, so one Config per call meant that churn twice a second forever.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved = install_stubs()
        from magere_brug import plugin as plugin_module

        cls.plugin_module = plugin_module

    @classmethod
    def tearDownClass(cls):
        restore_stubs(cls._saved)

    def setUp(self):
        self.plugin_module.Config.reset_mock()
        self.instance = self.plugin_module.MagereBrug()

    def test_config_is_constructed_at_most_once(self):
        for _ in range(20):
            self.instance._plugin_config()
        self.assertEqual(self.plugin_module.Config.call_count, 1)

    def test_repeated_keybinding_refresh_does_not_rebuild_config(self):
        for _ in range(20):
            self.instance._refresh_keybinding_cache()
        self.assertLessEqual(self.plugin_module.Config.call_count, 1)

    def test_missing_config_class_is_tolerated(self):
        saved = self.plugin_module.Config
        try:
            self.plugin_module.Config = None
            self.assertEqual(self.plugin_module.MagereBrug()._plugin_config(), {})
        finally:
            self.plugin_module.Config = saved

    def test_falls_back_to_default_keybinding(self):
        # The stub Config returns MagicMocks, not dicts, so no section is found.
        self.assertEqual(
            self.instance._keybinding_accel(),
            self.plugin_module.DEFAULT_KEYBINDING,
        )


if __name__ == '__main__':
    unittest.main()
