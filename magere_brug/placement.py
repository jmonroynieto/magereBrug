"""Window placement helpers for the navigator dialog."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from gi.repository import Gdk, GLib, Gtk

from magere_brug.constants import (
    NAV_HEIGHT,
    NAV_WIDTH,
    PLACE_CALLS_WARN,
    PLACE_RETRY_MS,
)
from magere_brug.log import get_logger

log = get_logger('placement')


class PlacementController:
    """Centers an undecorated dialog on its parent (or monitor workarea).

    Window managers often ignore a single ``move()``; this re-asserts position
    across map/allocate/idle/timeouts, then disconnects.

    ``place()`` is called from a ``size-allocate`` handler and itself calls
    ``resize()``/``move_resize()``, which can provoke another allocation. The
    ``_placing`` re-entrancy guard stops that becoming a nested cycle, and
    ``_place_calls`` counts invocations per scheduling round so the log can
    show whether the WM and this controller are fighting.
    """

    def __init__(self) -> None:
        self._window: Optional[Gtk.Window] = None
        self._parent: Optional[Gtk.Window] = None
        self._handler_ids: List[int] = []
        self._timeout_ids: List[int] = []
        self._placing = False
        self._place_calls = 0

    def cancel(self) -> None:
        if self._window is not None:
            for hid in self._handler_ids:
                try:
                    self._window.disconnect(hid)
                except (TypeError, AttributeError):
                    pass
        self._handler_ids = []
        for tid in self._timeout_ids:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._timeout_ids = []
        self._parent = None
        if self._place_calls:
            # High counts here mean the WM is rejecting our geometry and we are
            # re-asserting it in a loop — the prime suspect for render freezes.
            # Individual calls are usually under the span threshold, so without
            # this line a stall would show up with no attribution.
            if self._place_calls > PLACE_CALLS_WARN:
                log.warn('place.round_finished', calls=self._place_calls)
            else:
                log.debug('place.round_finished', calls=self._place_calls)
            self._place_calls = 0

    def schedule(self, window: Gtk.Window, parent: Optional[Gtk.Window]) -> None:
        self.cancel()
        self._window = window
        self._parent = parent
        try:
            self._handler_ids.append(window.connect('map-event', self._on_map))
        except Exception:
            pass
        try:
            self._handler_ids.append(
                window.connect('size-allocate', self._on_size_allocate)
            )
        except Exception:
            pass

        self.place()
        for delay in PLACE_RETRY_MS:
            if delay <= 0:
                GLib.idle_add(self.place)
            else:
                self._timeout_ids.append(GLib.timeout_add(delay, self.place))

        def cleanup() -> bool:
            self.place()
            if self._window is window:
                for hid in list(self._handler_ids):
                    try:
                        window.disconnect(hid)
                    except (TypeError, AttributeError):
                        pass
                self._handler_ids = []
            return False

        self._timeout_ids.append(
            GLib.timeout_add(max(PLACE_RETRY_MS) + 50, cleanup)
        )

    def place(self, window: Optional[Gtk.Window] = None, parent: Any = None) -> bool:
        """Center once. Returns False for GLib.idle_add / timeout_add."""
        window = window if window is not None else self._window
        parent = parent if parent is not None else self._parent
        if window is None or not isinstance(window, Gtk.Window):
            return False
        if self._window is not None and self._window is not window:
            return False
        # _force() resizes, which can re-enter here via size-allocate. Skipping
        # the nested call cannot change where the window lands — the outer call
        # is still going to apply its geometry.
        if self._placing:
            log.debug('place.reentrant_skipped')
            return False

        self._placing = True
        self._place_calls += 1
        try:
            with log.span('place') as span:
                w, h = self._pixel_size(window)
                pos = (
                    self._center_on_parent(parent, w, h)
                    if parent is not None
                    else None
                )
                span['on_parent'] = pos is not None
                if pos is None:
                    monitor = self._target_monitor(parent)
                    if monitor is None:
                        span['result'] = 'no_monitor'
                        return False
                    pos = self._center_on_monitor(monitor, w, h)
                x, y = pos
                span.update({'x': x, 'y': y, 'w': w, 'h': h})
                self._force(window, x, y, w, h)
        except Exception:
            log.exception('place.failed')
        finally:
            self._placing = False
        return False

    def _on_map(self, window: Gtk.Window, _event: Any) -> bool:
        log.debug('place.on_map')
        self.place(window, self._parent)
        return False

    def _on_size_allocate(self, window: Gtk.Window, _allocation: Any) -> None:
        # Re-entrant by construction: place() resizes, which allocates again.
        # place()'s guard breaks the recursion; this line lets the log show how
        # often the WM bounces us.
        log.debug('place.on_size_allocate')
        self.place(window, self._parent)
        try:
            alloc = window.get_allocation()
            if alloc.width <= 1 or alloc.height <= 1:
                return
            for hid in list(self._handler_ids):
                try:
                    window.disconnect(hid)
                except (TypeError, AttributeError):
                    pass
                if hid in self._handler_ids:
                    self._handler_ids.remove(hid)
        except Exception:
            log.exception('place.allocate_cleanup_failed')

    @staticmethod
    def _pixel_size(window: Gtk.Window) -> Tuple[int, int]:
        w = h = 0
        try:
            alloc = window.get_allocation()
            if alloc.width > 1:
                w = alloc.width
            if alloc.height > 1:
                h = alloc.height
        except Exception:
            pass
        try:
            gdk_win = window.get_window()
            if gdk_win is not None:
                if gdk_win.get_width() > 1:
                    w = max(w, gdk_win.get_width())
                if gdk_win.get_height() > 1:
                    h = max(h, gdk_win.get_height())
        except Exception:
            pass
        if w <= 1 or h <= 1:
            try:
                min_req, nat_req = window.get_preferred_size()
                w = max(w, getattr(nat_req, 'width', 0), getattr(min_req, 'width', 0))
                h = max(h, getattr(nat_req, 'height', 0), getattr(min_req, 'height', 0))
            except Exception:
                pass
        return int(w if w > 1 else NAV_WIDTH), int(h if h > 1 else NAV_HEIGHT)

    @staticmethod
    def _target_monitor(parent: Optional[Gtk.Window]) -> Optional[Gdk.Monitor]:
        display = Gdk.Display.get_default()
        if display is None:
            return None
        monitor = None
        if parent is not None:
            try:
                gdk_win = parent.get_window()
                if gdk_win is not None:
                    monitor = display.get_monitor_at_window(gdk_win)
            except Exception:
                pass
            if monitor is None:
                try:
                    px, py = parent.get_position()
                    monitor = display.get_monitor_at_point(int(px), int(py))
                except Exception:
                    pass
        if monitor is None:
            try:
                seat = display.get_default_seat()
                if seat is not None:
                    pointer = seat.get_pointer()
                    if pointer is not None:
                        _screen, px, py = pointer.get_position()
                        monitor = display.get_monitor_at_point(int(px), int(py))
            except Exception:
                pass
        if monitor is None:
            try:
                monitor = display.get_primary_monitor()
            except Exception:
                pass
        return monitor

    @staticmethod
    def _center_on_monitor(monitor: Gdk.Monitor, w: int, h: int) -> Tuple[int, int]:
        try:
            geo = monitor.get_workarea()
        except Exception:
            geo = monitor.get_geometry()
        return (
            int(geo.x + max(0, (geo.width - w) // 2)),
            int(geo.y + max(0, (geo.height - h) // 2)),
        )

    @staticmethod
    def _center_on_parent(
        parent: Gtk.Window, w: int, h: int
    ) -> Optional[Tuple[int, int]]:
        try:
            px = py = pw = ph = None
            gdk_win = parent.get_window()
            if gdk_win is not None:
                origin = gdk_win.get_origin()
                if isinstance(origin, tuple) and len(origin) == 3:
                    _ok, px, py = origin
                elif isinstance(origin, tuple) and len(origin) == 2:
                    px, py = origin
                else:
                    px, py = parent.get_position()
                pw, ph = gdk_win.get_width(), gdk_win.get_height()
            if px is None or py is None or pw is None or ph is None or pw <= 1 or ph <= 1:
                px, py = parent.get_position()
                pw = parent.get_allocated_width()
                ph = parent.get_allocated_height()
                if pw <= 1 or ph <= 1:
                    try:
                        req_w, req_h = parent.get_size()
                        pw = pw if pw > 1 else req_w
                        ph = ph if ph > 1 else req_h
                    except Exception:
                        pass
            if pw is None or ph is None or pw <= 1 or ph <= 1:
                return None
            return (
                int(px) + max(0, (int(pw) - w) // 2),
                int(py) + max(0, (int(ph) - h) // 2),
            )
        except Exception:
            return None

    @staticmethod
    def _force(window: Gtk.Window, x: int, y: int, w: int, h: int) -> None:
        with log.swallow('place.set_gravity'):
            window.set_gravity(Gdk.Gravity.NORTH_WEST)
        for name, action in (
            ('resize', lambda: window.resize(w, h)),
            ('move', lambda: window.move(x, y)),
        ):
            with log.swallow('place.' + name):
                action()
        with log.swallow('place.move_resize'):
            gdk_win = window.get_window()
            if gdk_win is not None:
                gdk_win.move_resize(x, y, w, h)
        try:
            geom = Gdk.Geometry()
            geom.min_width = geom.max_width = geom.base_width = w
            geom.min_height = geom.max_height = geom.base_height = h
            geom.win_gravity = Gdk.Gravity.NORTH_WEST
            hints = (
                Gdk.WindowHints.MIN_SIZE
                | Gdk.WindowHints.MAX_SIZE
                | Gdk.WindowHints.BASE_SIZE
                | Gdk.WindowHints.WIN_GRAVITY
            )
            window.set_geometry_hints(None, geom, hints)
        except Exception:
            log.exception('place.geometry_hints_failed')
