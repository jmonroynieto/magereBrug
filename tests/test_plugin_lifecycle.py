"""Regression tests for the two defects behind the 6-day main-loop stall.

Both are invisible to a short run: they only compound across plugin reloads,
which is exactly when nobody is watching. See DEBUGGING.md, suspect 2.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from test_plugin_import import install_stubs, restore_stubs  # noqa: E402


class FakeSignalObject(object):
    """Minimal GObject stand-in that actually tracks connected handlers."""

    def __init__(self):
        self.connected = {}
        self._next_id = 1

    def connect(self, name, callback, *args):
        handler_id = self._next_id
        self._next_id += 1
        self.connected[handler_id] = name
        return handler_id

    def disconnect(self, handler_id):
        del self.connected[handler_id]

    def handler_is_connected(self, handler_id):
        return handler_id in self.connected

    def count(self, name):
        return sum(1 for n in self.connected.values() if n == name)


class FakeTerminal(FakeSignalObject):
    def __init__(self):
        FakeSignalObject.__init__(self)
        self.vte = FakeSignalObject()

    def get_toplevel(self):
        return None


class PluginTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = install_stubs()
        from magere_brug import plugin as plugin_module

        cls.plugin_module = plugin_module

    @classmethod
    def tearDownClass(cls):
        restore_stubs(cls._saved)

    def _new(self):
        instance = self.plugin_module.MagereBrug()
        self.addCleanup(self._retire, instance)
        return instance

    @staticmethod
    def _retire(instance):
        import atexit

        try:
            atexit.unregister(instance._write_session_end)
        except Exception:
            pass


class TestSetupIsNotArmedOnADeadInstance(PluginTestCase):
    """An instance unloaded before its idle callback must install no timers.

    Terminator's ``load_plugins(force=True)`` unloads the old instance and
    constructs a new one synchronously, while the old one's
    ``GLib.idle_add(_deferred_setup)`` is still queued. Setting up after
    unload() leaves a hook timer and a watchdog that nothing can ever remove:
    every reload used to add another pair, and they all keep ticking for the
    life of the process.
    """

    def setUp(self):
        self.GLib = self.plugin_module.GLib

    def test_setup_after_unload_installs_nothing(self):
        instance = self._new()
        instance.unload()
        self.GLib.timeout_add.reset_mock()

        # The queued idle callback now fires, after unload() has already run.
        self.assertFalse(instance._deferred_setup())

        self.assertEqual(self.GLib.timeout_add.call_count, 0)
        self.assertIsNone(instance._hook_timeout_id)
        self.assertIsNone(instance._watchdog_timeout_id)

    def test_unload_cancels_the_pending_setup_callback(self):
        instance = self._new()
        pending = instance._setup_idle_id
        self.assertIsNotNone(pending)
        self.GLib.source_remove.reset_mock()

        instance.unload()

        removed = [call.args[0] for call in self.GLib.source_remove.call_args_list]
        self.assertIn(pending, removed)
        self.assertIsNone(instance._setup_idle_id)

    def test_setup_is_idempotent(self):
        instance = self._new()
        instance._deferred_setup()
        after_first = self.GLib.timeout_add.call_count
        self.assertGreaterEqual(after_first, 2)  # hook timer + watchdog

        instance._deferred_setup()

        self.assertEqual(self.GLib.timeout_add.call_count, after_first)


class TestRebindingDoesNotStackHandlers(PluginTestCase):
    """Rebinding must replace the previous owner's handler, not stack on it.

    The owner tag tells a new instance that someone else bound the handler but
    not how to remove it, so each rebind used to add another handler that
    nothing ever took off. With two instances ticking against the same
    terminal the count grew without bound, and every keystroke had to be
    dispatched through the whole list.
    """

    def test_two_owners_ticking_leave_one_handler_each(self):
        term = FakeTerminal()
        first, second = self._new(), self._new()

        for _ in range(50):
            first._hook_one_terminal(term)
            second._hook_one_terminal(term)

        self.assertEqual(term.vte.count('key-press-event'), 1)
        self.assertEqual(term.count('focus-in-event'), 1)

    def test_repeated_ticks_by_one_owner_bind_once(self):
        term = FakeTerminal()
        instance = self._new()

        for _ in range(50):
            instance._hook_one_terminal(term)

        self.assertEqual(term.vte.count('key-press-event'), 1)
        self.assertEqual(term.count('focus-in-event'), 1)

    def test_unhook_removes_the_handlers_it_bound(self):
        term = FakeTerminal()
        instance = self._new()

        instance._hook_one_terminal(term)
        instance._unhook_terminal(term)

        self.assertEqual(term.vte.connected, {})
        self.assertEqual(term.connected, {})


if __name__ == '__main__':
    unittest.main()
