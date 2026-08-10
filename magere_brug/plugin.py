"""MagereBrug Terminator plugin — terminal navigator UI and bindings."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gdk, GLib, Gtk, Pango

import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator
from terminatorlib.util import dbg, err

try:
    from terminatorlib.config import Config
except Exception:  # pragma: no cover
    Config = None  # type: ignore

from magere_brug import __version__
from magere_brug.constants import (
    DEFAULT_KEYBINDING,
    FOCUS_OUT_ARM_MS,
    HINT_TEXT,
    HOOK_INTERVAL_MS,
    NAV_HEIGHT,
    NAV_WIDTH,
    PLUGIN_NAME,
    RENAME_MAX_LEN,
    RSS_SAMPLE_EVERY,
    WATCHDOG_INTERVAL_MS,
    WATCHDOG_SUMMARY_MS,
    WATCHDOG_THRESHOLD_MS,
)
from magere_brug.css import NAV_CSS
from magere_brug import log as mblog
from magere_brug.placement import PlacementController
from magere_brug.util import (
    cmdline_for_pid,
    filter_and_group,
    first_attr,
    sanitize_title,
    shorten_path,
)

log = mblog.get_logger('plugin')


def _add_class(widget: Gtk.Widget, name: str) -> None:
    try:
        widget.get_style_context().add_class(name)
    except Exception:
        pass


def _diagnose_command() -> str:
    """Absolute path to diagnose.py, so the printed command is copy-pasteable.

    The plugin is installed by copying, not by pip, so there is no console
    script and no guarantee the package is importable from the user's cwd.
    """
    try:
        import magere_brug.diagnose as diagnose_module

        return os.path.abspath(diagnose_module.__file__)
    except Exception:
        return '-m magere_brug.diagnose'


def _parse_accel(accel_str: str):
    """Return (keyval, modifier_mask) or (None, None)."""
    try:
        result = Gtk.accelerator_parse(accel_str)
        if isinstance(result, tuple) and len(result) == 3:
            ok, keyval, mods = result
            if not ok or not keyval:
                return None, None
            return keyval, mods
        if isinstance(result, tuple) and len(result) == 2:
            keyval, mods = result
            if not keyval:
                return None, None
            return keyval, mods
    except Exception as exc:
        err('magereBrug accel parse %r: %s' % (accel_str, exc))
    return None, None


class MagereBrug(plugin.MenuItem):
    """Terminal switcher: context menu + configurable hotkey (default Ctrl+P)."""

    capabilities = ['terminal_menu']

    def __init__(self) -> None:
        plugin.MenuItem.__init__(self)
        self.window: Optional[Gtk.Window] = None
        self.search_entry: Optional[Gtk.SearchEntry] = None
        self.list_box: Optional[Gtk.ListBox] = None
        self.hint_label: Optional[Gtk.Label] = None
        self.rename_bar: Optional[Gtk.Box] = None
        self.rename_entry: Optional[Gtk.Entry] = None
        self.rename_btn: Optional[Gtk.Button] = None

        self.terminals_data: List[Dict[str, Any]] = []
        self._visible_rows: List[Gtk.ListBoxRow] = []
        self._mru: List[Any] = []
        self._source_terminal = None
        self._rename_term = None  # terminal being renamed, if any

        self._hook_timeout_id: Optional[int] = None
        self._css_provider = None
        self._placement = PlacementController()
        self._focus_out_armed = False
        self._closing = False

        # Parsed hotkey cache; refreshed on the hook timer (HOOK_INTERVAL_MS),
        # not on every keypress — config re-reads are otherwise paid on every
        # key press in every terminal.
        self._cached_accel_str: Optional[str] = None
        self._cached_accel: Tuple[Optional[int], Optional[int]] = (None, None)
        self._config = None  # see _get_config()

        # Diagnostics. The watchdog measures main-loop responsiveness; the
        # tick counter paces the (cheap but not free) RSS sampling.
        self._watchdog_timeout_id: Optional[int] = None
        self._stalls = mblog.StallDetector(
            interval_ms=WATCHDOG_INTERVAL_MS,
            threshold_ms=WATCHDOG_THRESHOLD_MS,
            summary_ms=WATCHDOG_SUMMARY_MS,
        )
        self._watchdog_ticks = 0
        self._hook_ticks = 0
        self._opens = 0
        self._rehook_pending_id: Optional[int] = None

        GLib.idle_add(self._deferred_setup)

    # ---------------------------------------------------------------- lifecycle

    def _start_logging(self) -> None:
        """Bring up the log before anything else can want to write to it.

        The level is resolved from config exactly once here — reading it per
        call site would drag a Terminator ``Config()`` construction onto the
        main loop, which is precisely the cost we are hunting.
        """
        section = self._plugin_config()
        path = mblog.configure(
            level=section.get('log_level') if section else None,
            debug=section.get('debug') if section else None,
        )
        fields = mblog.session_fields()
        fields.update(version=__version__, log_file=path or '<none>')
        error = mblog.setup_error()
        if error:
            fields['setup_error'] = error
            err('magereBrug: file logging unavailable (%s)' % error)
        # Session start/end markers let `diagnose` spot a session that ended
        # without a shutdown line — the signature of a hard crash, which is
        # otherwise invisible because a GTK/VTE segfault logs nothing.
        log.warn(mblog.EVENT_SESSION_START, **fields)

    def _deferred_setup(self) -> bool:
        dbg('magereBrug: plugin setup')
        self._start_logging()
        self._ensure_css()
        self._refresh_keybinding_cache()
        self._hook_timeout_id = GLib.timeout_add(
            HOOK_INTERVAL_MS, self._hook_terminal_listeners
        )
        self._watchdog_timeout_id = GLib.timeout_add(
            WATCHDOG_INTERVAL_MS, self._on_watchdog_tick
        )
        self._hook_terminal_listeners()
        log.debug('setup.complete', hook_interval_ms=HOOK_INTERVAL_MS)
        return False

    def _on_watchdog_tick(self) -> bool:
        """Heartbeat: late firing means the main loop was blocked."""
        report = self._stalls.tick()
        if report is not None:
            log.warn('mainloop.stall', **report)
        self._watchdog_ticks += 1
        if self._watchdog_ticks % RSS_SAMPLE_EVERY == 0:
            log.debug(
                'process.rss',
                rss_kb=mblog.rss_kb(),
                hook_ticks=self._hook_ticks,
                opens=self._opens,
                terminals=len(self._all_terminals()),
                mru=len(self._mru),
            )
        return True

    def unload(self) -> None:
        for attr in ('_hook_timeout_id', '_watchdog_timeout_id', '_rehook_pending_id'):
            source_id = getattr(self, attr, None)
            if source_id is not None:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._placement.cancel()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
        try:
            for term in self._all_terminals():
                self._unhook_terminal(term)
        except Exception as exc:
            log.exception('unload.unhook_failed')
            err('magereBrug unload: %s' % exc)
        log.warn(
            mblog.EVENT_SESSION_END,
            opens=self._opens,
            hook_ticks=self._hook_ticks,
            rss_kb=mblog.rss_kb(),
        )

    def callback(self, menuitems, menu, terminal) -> None:
        """Add the navigator item to the terminal context menu (3-arg API)."""
        item = Gtk.MenuItem.new_with_mnemonic('magereBrug _Navigator...')
        item.connect(
            'activate',
            lambda _w: self.toggle_navigator(source_terminal=terminal),
        )
        menuitems.append(item)

        diag = Gtk.MenuItem.new_with_mnemonic('magereBrug _Diagnostics...')
        diag.connect('activate', lambda _w: self._show_diagnostics(terminal))
        menuitems.append(diag)

    def _show_diagnostics(self, terminal=None) -> None:
        """Print the log location and a one-line health summary into the terminal.

        Deliberately writes into the user's own terminal rather than opening a
        dialog: it is the shortest path from "something froze" to a copyable
        log path, and it works even when the navigator window itself is broken.
        """
        path = mblog.log_path() or '<file logging unavailable>'
        lines = [
            'magereBrug %s — diagnostics' % __version__,
            '  log file : %s' % path,
            '  level    : set MAGEREBRUG_LOG_LEVEL=DEBUG (or debug=True in config)',
            '  report   : python3 %s' % _diagnose_command(),
            '  live tail: python3 %s --follow' % _diagnose_command(),
            '  opens=%d hook_ticks=%d terminals=%d rss_kb=%s'
            % (
                self._opens,
                self._hook_ticks,
                len(self._all_terminals()),
                mblog.rss_kb(),
            ),
        ]
        log.warn('diagnostics.requested', log_file=path)
        text = '\n'.join(lines) + '\n'
        vte = getattr(terminal, 'vte', None) if terminal is not None else None
        with log.swallow('diagnostics.feed'):
            if vte is not None and hasattr(vte, 'feed'):
                vte.feed(text.replace('\n', '\r\n').encode('utf-8'))
                return
        err(text)

    # ---------------------------------------------------------- config / keys

    def _get_config(self):
        """One cached Terminator ``Config``, built at most once per session.

        ``Config()`` is not the cheap accessor it looks like: its constructor
        calls ``connect_gsetting_callbacks()``, which creates two
        ``Gio.Settings`` objects and connects three signal handlers — every
        time. Constructing one per keybinding refresh meant doing that twice a
        second for the life of the process, each instance holding live dconf
        watches in a reference cycle until the cyclic collector got to it.

        Caching costs nothing in freshness: the underlying ``ConfigBase`` is a
        Borg whose ``load()`` short-circuits once loaded, so every instance
        reads the same in-memory tree regardless. Changes made through
        Preferences still land, because they mutate that shared tree.
        """
        if Config is None:
            return None
        if self._config is None:
            with log.span('config.construct'):
                self._config = Config()
        return self._config

    def _plugin_config(self) -> dict:
        try:
            cfg = self._get_config()
            if cfg is None:
                return {}
            if hasattr(cfg, 'plugin_get_config'):
                section = cfg.plugin_get_config(PLUGIN_NAME)
                if isinstance(section, dict):
                    return section
            plugins = None
            try:
                plugins = cfg['plugins']
            except Exception:
                plugins = getattr(cfg, 'plugin_config', None)
            if isinstance(plugins, dict):
                section = plugins.get(PLUGIN_NAME) or plugins.get('magereBrug')
                if isinstance(section, dict):
                    return section
        except Exception as exc:
            log.exception('config.read_failed')
            err('magereBrug config: %s' % exc)
        return {}

    def _keybinding_accel(self) -> str:
        section = self._plugin_config()
        for key in ('keybinding', 'hotkey', 'accel'):
            val = section.get(key) if section else None
            if val:
                return str(val).strip()
        return DEFAULT_KEYBINDING

    def _refresh_keybinding_cache(self) -> None:
        """Re-parse the configured accelerator.

        Called from the hook timer rather than per keypress; with the config
        instance cached this is a dict lookup, so it stays off the key path
        without costing anything on the timer.
        """
        accel_str = self._keybinding_accel()
        if accel_str == self._cached_accel_str and self._cached_accel[0] is not None:
            return
        keyval, mods = _parse_accel(accel_str)
        if keyval is None:
            log.warn('keybinding.unparseable', accel=accel_str)
            keyval, mods = _parse_accel(DEFAULT_KEYBINDING)
        self._cached_accel_str = accel_str
        self._cached_accel = (keyval, mods)
        log.debug('keybinding.updated', accel=accel_str)

    def _event_matches_hotkey(self, event) -> bool:
        keyval, mods = self._cached_accel
        if keyval is None:
            self._refresh_keybinding_cache()
            keyval, mods = self._cached_accel
            if keyval is None:
                return False
        mask = Gtk.accelerator_get_default_mod_mask()
        return (
            Gdk.keyval_to_lower(event.keyval) == Gdk.keyval_to_lower(keyval)
            and (event.state & mask) == ((mods or 0) & mask)
        )

    # ------------------------------------------------------------- terminals

    @staticmethod
    def _all_terminals() -> list:
        try:
            return list(getattr(Terminator(), 'terminals', []) or [])
        except Exception:
            return []

    def _hook_terminal_listeners(self) -> bool:
        # Runs every HOOK_INTERVAL_MS on the main thread, forever. Anything
        # expensive in here is felt as a periodic hitch, so it is spanned.
        self._hook_ticks += 1
        try:
            with log.span('hook.tick') as span:
                self._refresh_keybinding_cache()
                terminals = self._all_terminals()
                for term in terminals:
                    self._hook_one_terminal(term)
                self._hook_windows()
                span['terminals'] = len(terminals)
        except Exception as exc:
            log.exception('hook.tick_failed')
            err('magereBrug hook listeners: %s' % exc)
        return True

    def _hook_windows(self) -> None:
        seen = set()
        for term in self._all_terminals():
            try:
                top = term.get_toplevel()
            except Exception:
                continue
            if top is None or id(top) in seen:
                continue
            seen.add(id(top))
            # Owner-tagged (not a bare bool) so a plugin reload that skips
            # unload() still rebinds under the new instance instead of
            # silently finding the terminal "already hooked".
            if getattr(top, '_magere_brug_win_bound_owner', None) is self:
                continue
            with log.swallow('hook.window_bind'):
                top.connect('add', self._on_window_add)
                top._magere_brug_win_bound_owner = self
                log.debug('hook.window_bound')

    def _on_window_add(self, *_args) -> None:
        """A widget was added to a Terminator window — rescan for new terminals.

        'add' fires for every widget in a split, so one user action can emit a
        burst. Coalesce into a single pending rescan; the previous version
        queued an independent 100ms timeout per signal, each running a full
        hook pass.
        """
        if self._rehook_pending_id is not None:
            return
        self._rehook_pending_id = GLib.timeout_add(100, self._hook_once)

    def _hook_once(self) -> bool:
        self._rehook_pending_id = None
        self._hook_terminal_listeners()
        return False

    def _hook_one_terminal(self, term) -> None:
        vte = getattr(term, 'vte', None)
        if vte is None:
            return
        # Bound flags are tagged with the owning plugin instance rather than
        # a bare bool: if Terminator ever re-instantiates the plugin without
        # calling unload() first, a stale flag from the old instance must not
        # cause the new instance to skip binding its own handler.
        if getattr(vte, '_magere_brug_bound_owner', None) is not self:
            try:
                vte.connect('key-press-event', self._on_vte_key_press)
                vte._magere_brug_bound_owner = self
            except Exception as exc:
                err('magereBrug bind key: %s' % exc)
        if getattr(term, '_magere_brug_focus_bound_owner', None) is not self:
            try:
                term.connect('focus-in-event', self._on_terminal_focus_in)
                term._magere_brug_focus_bound_owner = self
            except Exception:
                try:
                    vte.connect(
                        'focus-in-event',
                        lambda _w, _e, t=term: self._note_mru(t) or False,
                    )
                    term._magere_brug_focus_bound_owner = self
                except Exception as exc:
                    err('magereBrug bind focus: %s' % exc)
        if term not in self._mru:
            self._mru.append(term)

    def _unhook_terminal(self, term) -> None:
        vte = getattr(term, 'vte', None)
        if vte is not None and getattr(vte, '_magere_brug_bound_owner', None) is self:
            try:
                vte.disconnect_by_func(self._on_vte_key_press)
            except (TypeError, AttributeError):
                pass
            vte._magere_brug_bound_owner = None
        if getattr(term, '_magere_brug_focus_bound_owner', None) is self:
            try:
                term.disconnect_by_func(self._on_terminal_focus_in)
            except (TypeError, AttributeError):
                pass
            term._magere_brug_focus_bound_owner = None

    def _on_terminal_focus_in(self, term, _event) -> bool:
        self._note_mru(term)
        return False

    def _note_mru(self, term) -> bool:
        if term is None:
            return False
        try:
            self._mru = [t for t in self._mru if t is not term and self._term_alive(t)]
            self._mru.insert(0, term)
        except Exception:
            pass
        return False

    @staticmethod
    def _term_alive(term) -> bool:
        try:
            return term is not None and getattr(term, 'vte', None) is not None
        except Exception:
            return False

    def _on_vte_key_press(self, widget, event) -> bool:
        """Intercept hotkey only when two or more terminals exist."""
        try:
            if not self._event_matches_hotkey(event):
                return False
            if len(self._all_terminals()) <= 1:
                return False
            source = self._terminal_for_vte(widget)
            log.debug('hotkey.matched')
            GLib.idle_add(
                lambda s=source: self.toggle_navigator(source_terminal=s) or False
            )
            return True
        except Exception as exc:
            log.exception('keypress.failed')
            err('magereBrug keypress: %s' % exc)
        return False

    def _terminal_for_vte(self, vte_widget):
        for term in self._all_terminals():
            if getattr(term, 'vte', None) is vte_widget:
                return term
        return None

    def _current_terminal(self):
        if self._source_terminal is not None and self._term_alive(self._source_terminal):
            return self._source_terminal
        for term in self._mru:
            if self._term_alive(term):
                return term
        terms = self._all_terminals()
        return terms[0] if terms else None

    def _previous_terminal(self, current=None):
        current = current or self._current_terminal()
        for term in self._mru:
            if self._term_alive(term) and term is not current:
                return term
        for term in self._all_terminals():
            if term is not current:
                return term
        return None

    # --------------------------------------------------------- metadata

    def _terminal_title(self, term) -> str:
        title = first_attr(term, ('get_window_title',))
        if title:
            return str(title)
        titlebar = getattr(term, 'titlebar', None)
        if titlebar is not None:
            custom = first_attr(titlebar, ('get_custom_string',))
            if custom:
                return str(custom)
        vte = getattr(term, 'vte', None)
        if vte is not None:
            t = first_attr(vte, ('get_window_title',))
            if t:
                return str(t)
        return 'Unnamed Terminal'

    def _terminal_cwd(self, term) -> Optional[str]:
        cwd = first_attr(term, ('get_cwd', 'cwd'))
        if cwd:
            return shorten_path(str(cwd))
        vte = getattr(term, 'vte', None)
        if vte is not None and hasattr(vte, 'get_current_directory_uri'):
            try:
                uri = vte.get_current_directory_uri()
                if uri:
                    if uri.startswith('file://'):
                        path = GLib.filename_from_uri(uri)[0]
                    else:
                        path = uri
                    return shorten_path(path)
            except Exception:
                pass
        return None

    def _terminal_command(self, term) -> Optional[str]:
        pid = first_attr(term, ('pid', 'get_pid'))
        vte = getattr(term, 'vte', None)
        if pid is None and vte is not None:
            pid = first_attr(vte, ('get_pid', 'pid'))
        if pid:
            cmd = cmdline_for_pid(pid)
            if cmd:
                return cmd
        if vte is not None:
            t = first_attr(vte, ('get_window_title',))
            if t:
                return str(t)
        return None

    def _window_title_for_term(self, term) -> str:
        try:
            parent = term.get_toplevel()
            if parent is not None and hasattr(parent, 'get_title'):
                t = parent.get_title()
                if t:
                    return t
        except Exception:
            pass
        return 'Window'

    def _window_key_for_term(self, term):
        try:
            top = term.get_toplevel()
            if top is not None:
                return id(top)
        except Exception:
            pass
        return 0

    def _mru_index(self, term) -> int:
        try:
            return self._mru.index(term)
        except ValueError:
            return 10 ** 6

    def _gather_terminals(self) -> None:
        # Reads /proc for every terminal (cmdline_for_pid) — cheap per call,
        # but it is on the open path, so a laggy navigator shows up here.
        self.terminals_data = []
        try:
            with log.span('gather_terminals') as span:
                self._gather_terminals_inner()
                span['count'] = len(self.terminals_data)
        except Exception as exc:
            log.exception('gather_terminals.failed')
            err('magereBrug gather terminals: %s' % exc)

    def _gather_terminals_inner(self) -> None:
        live = self._all_terminals()
        self._mru = [t for t in self._mru if t in live]
        for term in live:
            if term not in self._mru:
                self._mru.append(term)
            title = self._terminal_title(term)
            cwd = self._terminal_cwd(term)
            cmd = self._terminal_command(term)
            win_title = self._window_title_for_term(term)
            bits = [b for b in (cwd, cmd if cmd != title else None) if b]
            detail = ' · '.join(bits) if bits else 'Idle'
            self.terminals_data.append({
                'object': term,
                'title': title,
                'context': '[%s] %s' % (win_title, detail),
                'cwd': cwd,
                'cmd': cmd,
                'win_title': win_title,
                'win_key': self._window_key_for_term(term),
                'search': ' '.join(
                    x for x in (title, win_title, cwd or '', cmd or '', detail) if x
                ),
                'mru_index': self._mru_index(term),
            })

    def _resolve_parent_window(self, source_terminal=None) -> Optional[Gtk.Window]:
        candidates = []
        if source_terminal is not None:
            candidates.append(source_terminal)
        candidates.extend(self._all_terminals())
        for term in candidates:
            try:
                top = term.get_toplevel() if hasattr(term, 'get_toplevel') else None
                if top is not None and isinstance(top, Gtk.Window):
                    return top
            except Exception:
                continue
        return None

    # ---------------------------------------------------------------- theme

    def _ensure_css(self) -> None:
        if self._css_provider is not None:
            return
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(NAV_CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self._css_provider = provider
            log.debug('css.installed')
        except Exception as exc:
            log.exception('css.failed')
            err('magereBrug css: %s' % exc)

    # ----------------------------------------------------------- open / close

    def toggle_navigator(self, source_terminal=None) -> None:
        if self.window is not None and self.window.get_visible():
            log.debug('navigator.toggle_close')
            self._close_navigator(refocus=self._source_terminal or source_terminal)
            return

        self._opens += 1
        with log.span('navigator.open') as span:
            self._open_navigator(source_terminal)
            span['open_count'] = self._opens

    def _open_navigator(self, source_terminal=None) -> None:
        self._source_terminal = source_terminal or self._current_terminal()
        if self._source_terminal is not None:
            self._note_mru(self._source_terminal)

        self._gather_terminals()
        parent = self._resolve_parent_window(self._source_terminal)

        if self.window is None:
            self._build_window(parent)
        else:
            if parent is not None:
                try:
                    self.window.set_transient_for(parent)
                except Exception:
                    pass
            self._placement.schedule(self.window, parent)

        if self.search_entry is not None:
            self.search_entry.set_text('')
        self._exit_rename_mode()
        self._update_list_view()
        self.window.show_all()
        # Keep rename bar hidden until F2 / Rename is used.
        if self.rename_bar is not None:
            self.rename_bar.hide()
        self._placement.place(self.window, parent)
        try:
            self.window.present()
        except Exception:
            pass
        self._focus_out_armed = False
        GLib.timeout_add(FOCUS_OUT_ARM_MS, self._arm_focus_out)
        if self.search_entry is not None:
            self.search_entry.grab_focus()

    def _arm_focus_out(self) -> bool:
        self._focus_out_armed = True
        return False

    def _build_window(self, parent: Optional[Gtk.Window]) -> None:
        self._ensure_css()
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window = win
        win.set_title('magereBrug Navigator')
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_accept_focus(True)
        win.set_focus_on_map(True)
        win.set_resizable(False)
        win.set_default_size(NAV_WIDTH, NAV_HEIGHT)
        win.set_size_request(NAV_WIDTH, NAV_HEIGHT)
        # Soft modal: click-outside can dismiss via focus-out.
        win.set_modal(False)
        try:
            win.set_keep_above(True)
        except Exception:
            pass
        _add_class(win, 'magere-brug-nav')

        if parent is not None:
            win.set_transient_for(parent)
            win.set_destroy_with_parent(True)
            win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            win.set_position(Gtk.WindowPosition.CENTER)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(12)
        win.add(vbox)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            'Search title, window, path, or command...'
        )
        self.search_entry.connect('search-changed', self._on_search_changed)
        self.search_entry.connect('key-press-event', self._on_modal_key_press)
        vbox.pack_start(self.search_entry, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        vbox.pack_start(scrolled, True, True, 0)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect('row-activated', self._on_row_activated)
        self.list_box.connect('key-press-event', self._on_modal_key_press)
        scrolled.add(self.list_box)

        # Inline rename strip (hidden until F2 / Rename).
        self.rename_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        _add_class(self.rename_bar, 'magere-brug-rename-bar')
        rename_lbl = Gtk.Label(label='Rename', xalign=0)
        self.rename_entry = Gtk.Entry()
        self.rename_entry.set_placeholder_text('Terminal title…')
        self.rename_entry.set_hexpand(True)
        self.rename_entry.connect('key-press-event', self._on_rename_key_press)
        self.rename_entry.connect('activate', lambda *_a: self._commit_rename())
        apply_btn = Gtk.Button.new_with_label('Apply')
        apply_btn.connect('clicked', lambda *_a: self._commit_rename())
        cancel_btn = Gtk.Button.new_with_label('Cancel')
        cancel_btn.connect(
            'clicked', lambda *_a: self._exit_rename_mode(focus_search=True)
        )
        self.rename_bar.pack_start(rename_lbl, False, False, 0)
        self.rename_bar.pack_start(self.rename_entry, True, True, 0)
        self.rename_bar.pack_start(apply_btn, False, False, 0)
        self.rename_bar.pack_start(cancel_btn, False, False, 0)
        vbox.pack_start(self.rename_bar, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        _add_class(actions, 'magere-brug-actions')
        self.rename_btn = Gtk.Button.new_with_mnemonic('_Rename')
        self.rename_btn.set_tooltip_text('Rename selected terminal (F2)')
        self.rename_btn.connect('clicked', lambda *_a: self._start_rename())
        actions.pack_start(self.rename_btn, False, False, 0)
        self.hint_label = Gtk.Label(label=HINT_TEXT, xalign=0)
        self.hint_label.set_line_wrap(True)
        self.hint_label.set_hexpand(True)
        _add_class(self.hint_label, 'magere-brug-hint')
        actions.pack_start(self.hint_label, True, True, 0)
        vbox.pack_start(actions, False, False, 0)

        win.connect('key-press-event', self._on_modal_key_press)
        win.connect('focus-out-event', self._on_window_focus_out)
        win.connect('delete-event', self._on_delete)
        win.connect('destroy', lambda *_a: self._on_window_destroyed())

        self._placement.schedule(win, parent)

    def _on_window_destroyed(self) -> None:
        self._placement.cancel()
        self.window = None
        self.search_entry = None
        self.list_box = None
        self.hint_label = None
        self.rename_bar = None
        self.rename_entry = None
        self.rename_btn = None
        self._rename_term = None
        self._visible_rows = []

    def _on_delete(self, *_args) -> bool:
        self._close_navigator(refocus=self._source_terminal)
        return True

    def _on_window_focus_out(self, _window, _event) -> bool:
        if self._focus_out_armed and not self._closing:
            GLib.idle_add(self._maybe_close_on_focus_loss)
        return False

    def _maybe_close_on_focus_loss(self) -> bool:
        if self._closing or self.window is None:
            return False
        try:
            if self.window.get_visible() and not self.window.has_toplevel_focus():
                self._close_navigator(refocus=self._source_terminal)
        except Exception:
            pass
        return False

    def _close_navigator(self, refocus=None) -> None:
        if self._closing:
            return
        self._closing = True
        self._exit_rename_mode()
        self._placement.cancel()
        self._focus_out_armed = False
        win = self.window
        if win is not None:
            try:
                win.hide()
            except Exception:
                try:
                    win.destroy()
                    self.window = None
                except Exception:
                    pass
        self._closing = False
        if refocus is not None:
            GLib.idle_add(lambda t=refocus: self._focus_terminal(t) or False)

    def _focus_terminal(self, term) -> None:
        if term is None or not self._term_alive(term):
            log.debug('focus.skipped_dead')
            return
        with log.span('focus_terminal'):
            with log.swallow('focus.present'):
                top = term.get_toplevel()
                if top and isinstance(top, Gtk.Window):
                    top.present()
            for target in (term, getattr(term, 'vte', None)):
                if target is None:
                    continue
                with log.swallow('focus.grab'):
                    target.grab_focus()
        self._note_mru(term)

    # -------------------------------------------------------------- list UI

    def _on_search_changed(self, _entry) -> None:
        self._update_list_view()

    def _update_list_view(self) -> None:
        if not self.list_box:
            return
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        self._visible_rows = []

        query = self.search_entry.get_text().strip() if self.search_entry else ''
        groups = filter_and_group(self.terminals_data, query)
        current = self._current_terminal()

        if not groups:
            self._add_empty_row(
                'No matching terminals' if query else 'No terminals found'
            )
            self.list_box.show_all()
            return

        index = 0
        for group in groups.values():
            self.list_box.add(self._make_header_row(group['title']))
            for item in group['items']:
                index += 1
                row = self._make_term_row(item, current, index)
                self.list_box.add(row)
                self._visible_rows.append(row)

        self.list_box.show_all()
        if self._visible_rows:
            self.list_box.select_row(self._visible_rows[0])
            try:
                if self.search_entry is not None:
                    self.search_entry.grab_focus()
            except Exception:
                pass

    def _add_empty_row(self, message: str) -> None:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=message, xalign=0.5)
        _add_class(lbl, 'magere-brug-empty')
        lbl.set_margin_top(24)
        lbl.set_margin_bottom(24)
        row.add(lbl)
        self.list_box.add(row)

    def _make_header_row(self, title: str) -> Gtk.ListBoxRow:
        header = Gtk.ListBoxRow()
        header.set_selectable(False)
        header.set_activatable(False)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hbox.set_margin_start(8)
        hbox.set_margin_end(8)
        hbox.set_margin_top(6)
        hbox.set_margin_bottom(2)
        hlbl = Gtk.Label(label=title, xalign=0)
        _add_class(hlbl, 'magere-brug-header')
        hbox.pack_start(hlbl, True, True, 0)
        header.add(hbox)
        return header

    def _make_term_row(self, item, current, index: int) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.terminal_obj = item['object']
        row.is_term_row = True

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)

        if index <= 9:
            idx_lbl = Gtk.Label(label=str(index), xalign=0.5)
            _add_class(idx_lbl, 'magere-brug-row-index')
            outer.pack_start(idx_lbl, False, False, 0)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        is_current = current is not None and item['object'] is current
        title_esc = GLib.markup_escape_text(item['title'])
        if is_current:
            markup = (
                '<span font_weight="bold">%s</span>  '
                '<span size="small" alpha="60%%">current</span>' % title_esc
            )
        else:
            markup = title_esc

        lbl_title = Gtk.Label(xalign=0)
        lbl_title.set_use_markup(True)
        lbl_title.set_markup(markup)
        lbl_title.set_ellipsize(Pango.EllipsizeMode.END)
        if is_current:
            _add_class(lbl_title, 'magere-brug-current-title')

        lbl_ctx = Gtk.Label(label=item['context'], xalign=0)
        lbl_ctx.set_ellipsize(Pango.EllipsizeMode.END)
        _add_class(lbl_ctx, 'magere-brug-muted')

        col.pack_start(lbl_title, True, True, 0)
        col.pack_start(lbl_ctx, True, True, 0)
        outer.pack_start(col, True, True, 0)
        row.add(outer)
        return row

    # ----------------------------------------------------------- navigation

    def _selected_term_row(self) -> Optional[Gtk.ListBoxRow]:
        if not self.list_box:
            return None
        row = self.list_box.get_selected_row()
        if row is not None and getattr(row, 'is_term_row', False):
            return row
        return self._visible_rows[0] if self._visible_rows else None

    def _move_selection(self, delta: int) -> None:
        if not self._visible_rows or not self.list_box:
            return
        current = self.list_box.get_selected_row()
        try:
            idx = self._visible_rows.index(current)
        except ValueError:
            idx = 0
        idx = max(0, min(len(self._visible_rows) - 1, idx + delta))
        row = self._visible_rows[idx]
        self.list_box.select_row(row)
        try:
            if self.search_entry is not None:
                self.search_entry.grab_focus()
                self.search_entry.set_position(-1)
        except Exception:
            pass
        self._ensure_row_visible(row)

    def _ensure_row_visible(self, row: Gtk.ListBoxRow) -> None:
        try:
            parent = self.list_box.get_parent()
            if not isinstance(parent, Gtk.ScrolledWindow):
                return
            adj = parent.get_vadjustment()
            if adj is None:
                return
            alloc = row.get_allocation()
            value, page = adj.get_value(), adj.get_page_size()
            top, bottom = alloc.y, alloc.y + alloc.height
            if top < value:
                adj.set_value(top)
            elif bottom > value + page:
                adj.set_value(bottom - page)
        except Exception:
            pass

    def _activate_index(self, one_based: int) -> bool:
        if one_based < 1 or one_based > len(self._visible_rows):
            return False
        row = self._visible_rows[one_based - 1]
        self.list_box.select_row(row)
        self._on_row_activated(self.list_box, row)
        return True

    def _on_row_activated(self, _listbox, row) -> None:
        if not getattr(row, 'is_term_row', False):
            return
        if self._rename_term is not None:
            self._exit_rename_mode()
        self._close_navigator(refocus=getattr(row, 'terminal_obj', None))

    # --------------------------------------------------------------- rename

    def _start_rename(self, term=None) -> bool:
        """Show the rename bar for *term* or the selected row. Returns True if started."""
        if term is None:
            row = self._selected_term_row()
            if row is None:
                return False
            term = getattr(row, 'terminal_obj', None)
        if term is None or not self._term_alive(term):
            return False
        if self.rename_bar is None or self.rename_entry is None:
            return False

        self._rename_term = term
        current = self._terminal_title(term)
        # Prefer an existing custom label if present.
        titlebar = getattr(term, 'titlebar', None)
        if titlebar is not None:
            custom = first_attr(titlebar, ('get_custom_string', 'get_custom_label'))
            if custom:
                current = str(custom)

        self.rename_entry.set_text(current)
        self.rename_bar.show_all()
        self.rename_entry.grab_focus()
        self.rename_entry.select_region(0, -1)
        if self.rename_btn is not None:
            self.rename_btn.set_sensitive(False)
        return True

    def _exit_rename_mode(self, focus_search: bool = False) -> None:
        self._rename_term = None
        if self.rename_entry is not None:
            self.rename_entry.set_text('')
        if self.rename_bar is not None:
            self.rename_bar.hide()
        if self.rename_btn is not None:
            self.rename_btn.set_sensitive(True)
        if focus_search and self.search_entry is not None:
            try:
                self.search_entry.grab_focus()
            except Exception:
                pass

    def _commit_rename(self) -> bool:
        term = self._rename_term
        if term is None or self.rename_entry is None:
            self._exit_rename_mode(focus_search=True)
            return False
        name = sanitize_title(self.rename_entry.get_text(), RENAME_MAX_LEN)
        ok = self._set_terminal_title(term, name)
        log.debug('rename.commit', ok=ok, length=len(name))
        if not ok:
            log.warn('rename.failed', length=len(name))
            err('magereBrug: could not set terminal title')
        # Refresh list so the new title shows; stay in the navigator.
        self._gather_terminals()
        self._exit_rename_mode()
        self._update_list_view()
        self._select_terminal_row(term)
        if self.search_entry is not None:
            self.search_entry.grab_focus()
        return ok

    def _select_terminal_row(self, term) -> None:
        if not self.list_box or term is None:
            return
        for row in self._visible_rows:
            if getattr(row, 'terminal_obj', None) is term:
                self.list_box.select_row(row)
                self._ensure_row_visible(row)
                return

    def _set_terminal_title(self, term, name: str) -> bool:
        """Apply a custom title via Terminator titlebar APIs (best-effort)."""
        titlebar = getattr(term, 'titlebar', None)
        tried = False
        if titlebar is not None:
            for method in ('set_custom_string', 'set_custom_label', 'set_title'):
                if hasattr(titlebar, method):
                    try:
                        getattr(titlebar, method)(name if name else None)
                        tried = True
                        break
                    except TypeError:
                        try:
                            getattr(titlebar, method)(name)
                            tried = True
                            break
                        except Exception:
                            continue
                    except Exception as exc:
                        err('magereBrug title %s: %s' % (method, exc))
            if not tried:
                for attr in ('custom_string', 'customtext', 'custom_label'):
                    if hasattr(titlebar, attr):
                        try:
                            setattr(titlebar, attr, name if name else None)
                            tried = True
                            break
                        except Exception:
                            continue
            if tried and hasattr(titlebar, 'update'):
                try:
                    titlebar.update()
                except Exception:
                    pass
            if tried:
                return True

        for method in ('set_title', 'force_set_title'):
            if hasattr(term, method):
                try:
                    getattr(term, method)(name)
                    return True
                except Exception as exc:
                    err('magereBrug term.%s: %s' % (method, exc))
        return False

    def _on_rename_key_press(self, _widget, event) -> bool:
        keyval = event.keyval
        if keyval == Gdk.KEY_Escape:
            self._exit_rename_mode(focus_search=True)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._commit_rename()
            return True
        return False

    def _on_modal_key_press(self, _widget, event) -> bool:
        keyval = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()

        # While renaming, Esc/Enter apply to the rename bar; block palette nav.
        if self._rename_term is not None:
            if keyval == Gdk.KEY_Escape:
                self._exit_rename_mode(focus_search=True)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._commit_rename()
                return True
            if keyval in (
                Gdk.KEY_Down,
                Gdk.KEY_Up,
                Gdk.KEY_KP_Down,
                Gdk.KEY_KP_Up,
                Gdk.KEY_Page_Down,
                Gdk.KEY_Page_Up,
                Gdk.KEY_F2,
            ):
                return True

        if keyval == Gdk.KEY_Escape:
            self._close_navigator(refocus=self._source_terminal)
            return True

        if keyval == Gdk.KEY_F2:
            if self._start_rename():
                return True

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._handle_enter()
            return True

        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_selection(-1)
            return True
        if keyval == Gdk.KEY_Page_Down:
            self._move_selection(5)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self._move_selection(-5)
            return True
        if keyval == Gdk.KEY_Home and mods:
            self._move_selection(-(10 ** 6))
            return True
        if keyval == Gdk.KEY_End and mods:
            self._move_selection(10 ** 6)
            return True

        query = self.search_entry.get_text() if self.search_entry else ''
        if not query and mods == 0:
            digit = None
            if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
                digit = keyval - Gdk.KEY_0
            elif Gdk.KEY_KP_1 <= keyval <= Gdk.KEY_KP_9:
                digit = keyval - Gdk.KEY_KP_0
            if digit is not None and self._activate_index(digit):
                return True

        return False

    def _handle_enter(self) -> None:
        if self._rename_term is not None:
            self._commit_rename()
            return

        query = self.search_entry.get_text().strip() if self.search_entry else ''
        row = self._selected_term_row()
        first = self._visible_rows[0] if self._visible_rows else None

        if not query:
            prev = self._previous_terminal(self._source_terminal)
            if row is not None and first is not None and row is not first:
                self._on_row_activated(self.list_box, row)
            elif prev is not None:
                self._close_navigator(refocus=prev)
            elif row is not None:
                self._on_row_activated(self.list_box, row)
            else:
                self._close_navigator(refocus=self._source_terminal)
            return

        if row is not None:
            self._on_row_activated(self.list_box, row)
        else:
            self._close_navigator(refocus=self._source_terminal)
