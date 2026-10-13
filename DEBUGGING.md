# Debugging magereBrug freezes and crashes

Working notes on why this plugin might be stalling Terminator, what evidence
would settle each theory, and what the instrumentation can and cannot see.

**Status:** found, 2026-08-28. The cause was not on the suspect list: plugin
reloads were leaking timers, and orphaned timers were leaking signal handlers.
See [The confirmed cause](#the-confirmed-cause-leaked-timers-and-leaked-handlers).
The suspects below are kept as written, because the reasoning that ruled them
out is the useful part.

## The confirmed cause: leaked timers and leaked handlers

Two defects that compound only across plugin reloads. A short session shows
neither, which is why reading the code found the wrong suspects.

### Evidence

A Terminator process up 6d12h, RSS 1.14 GB, 4.3% CPU sustained, with four
terminals open. From `diagnose`:

- `hook.tick.slow count=30558 max=593ms total=5706705ms`
- `mainloop.stall reports=1239 stalls=13605 worst=24933ms`

The tick is supposed to run every 1500 ms. Counting ticks in the newest log
gives 2.009/s against an expected 0.667/s — **3.01 timers** — while the log
contains exactly one live plugin instance (last `session.start` 15:52:25, no
`session.end` after it). Three ticks arrive back-to-back, each starting as the
previous one ends:

```text
17:52:49.047 hook.tick.slow terminals=4 duration_ms=133.9
17:52:49.169 hook.tick.slow terminals=4 duration_ms=122.3
17:52:49.291 hook.tick.slow terminals=4 duration_ms=121.4
17:52:50.549 …                                       ← next burst, +1502ms
```

Three watchdogs report the same stall at the same instant with max_ms 387.1,
387.2, 387.3. About 400 ms of every 1500 ms went to hook ticks — a 27% duty
cycle on the main loop, felt as typing lag in every terminal.

`session.end` markers show RSS climbing monotonically across reloads:
726 MB → 772 MB → 934 MB → 1022 MB, and 1166 MB by the time it was measured.

### Defect 1 — setup arming an already-unloaded instance

`__init__` queues `GLib.idle_add(self._deferred_setup)`. Terminator's
`load_plugins(force=True)` (`terminatorlib/plugin.py:105`) calls `unload()` on
the old instance and constructs the new one synchronously — while the old
instance's idle callback is still queued.

`unload()` removed `_hook_timeout_id` and `_watchdog_timeout_id`, but at that
point they were still `None`: setup had not run yet. The idle callback then
fired on the dead instance and installed both timers, and nothing holds a
reference that could ever remove them. Each reload leaked another pair.

Per-instance arithmetic confirms each *live* instance ran exactly one timer —
`hook_ticks=1189` over 1796 s, `1643` over 2466 s, `2414` over 3647 s, all
0.66/s. The surplus ticks belonged to instances that had already logged
`session.end`.

**Fixed** by latching an `_unloaded` flag, keeping the idle source id so
`unload()` can cancel it, and making `_deferred_setup` bail if it is already
armed.

### Defect 2 — the owner tag could not clean up after itself

Handlers were tagged with the owning instance (`_magere_brug_bound_owner`) so
a reload would rebind rather than skip. But the tag records *that* someone else
bound the handler, not *which* handler to remove — so `_hook_one_terminal`
connected a new one and left the old one attached.

With one instance this is harmless: the tag matches and nothing rebinds. With
the orphaned timers from defect 1 it becomes a ping-pong. Instance A's tick
sets the tag to A; instance C's tick sees "not C", connects another handler,
sets the tag to C; A's next tick sees "not A"… Every tick, every orphan, every
terminal added one `key-press-event` handler that was never removed. A test
running two owners for 50 rounds produced 100 handlers where there should be 1.

The unbounded growth is proven (100 handlers where there should be 1) and
accounts for the memory: every keystroke in every terminal is dispatched
through that list by construction. The ~130 ms per tick is *consistent* with
`connect()` walking the same list — GObject's `handler_insert` scans — but that
was never measured, and one fact sits against it: the duration stayed flat near
130 ms while the terminal count fell from 10 to 4, which a pure per-terminal
cost would not do. Treat the per-tick constant as unexplained. It does not
affect either fix; both defects stand on their own evidence.

`_unhook_terminal` had a matching bug — the fallback path binds a lambda to the
*vte*, but cleanup called `term.disconnect_by_func(self._on_terminal_focus_in)`,
which never matched anything.

**Fixed** by recording the handler id next to the owner tag and disconnecting
by id before rebinding. Regression tests are in `tests/test_plugin_lifecycle.py`.

### What this says about the old suspects

Suspect 1 (placement) was never implicated: no `place.*` events appear near any
stall. Suspect 2 named the right function for the wrong reason — the cost was
never the `Config()` construction, it was the handler list the tick was
extending. Suspect 4's note that `_on_vte_key_press` "runs on every keystroke
in every terminal" was the right instinct; what it missed is that there was
more than one of them.

---

## Capturing a freeze

Default logging is enough. Stalls, slow spans and the placement-loop signature
all report at WARNING:

```bash
python3 ~/.config/terminator/plugins/magere_brug/diagnose.py
```

If the summary is unhelpful, reproduce once more with everything on:

```bash
MAGEREBRUG_LOG_LEVEL=DEBUG terminator
```

DEBUG adds per-call detail — every `place()` invocation, every hook tick, every
swallowed exception. It is loud enough to slow things down slightly, so it is
for a targeted repro, not for daily use.

## Triage

| `diagnose` shows | Read it as |
|---|---|
| No `mainloop.stall` at all | The GTK main loop was never blocked. The freeze is not this plugin's Python — look at VTE, the WM, or the GPU driver. |
| `mainloop.stall` + `place.round_finished` (WARN) nearby | Suspect 1. Strong. |
| `mainloop.stall` + `place.slow` | Suspect 1, but a slow single call rather than a loop. Check `duration_ms`. |
| `mainloop.stall` + `hook.tick.slow` | Suspect 2. |
| `mainloop.stall` + `gather_terminals.slow` | Suspect 3. |
| `mainloop.stall` alone, nothing else | Suspect 4 — something not yet instrumented. Re-run at DEBUG. |
| `session.start` with no `session.end` | The process did not exit cleanly. See "What this cannot see". |

`mainloop.stall`'s `max_ms` is the *blocked* time, not elapsed time: the
watchdog beats every 500ms, and the overshoot beyond that is the freeze.

---

## Suspect 1 — the placement resize loop

**Leading theory.** `magere_brug/placement.py`

### Mechanism

`PlacementController.schedule()` connects `size-allocate`
(`placement.py:77`). The handler calls `place()` (`placement.py:150`), which
calls `_force()` (`placement.py:283`), which calls `window.resize()`,
`gdk_win.move_resize()` and `window.set_geometry_hints()`.

Resizing a window causes an allocation. An allocation calls the handler. That
is a cycle, and it is exactly the shape that produces an unresponsive UI and a
window that visibly flickers or refuses to settle.

It is made more likely by what this controller is deliberately doing: the
window is undecorated and `set_keep_above(True)` (`plugin.py:778`), and the
controller re-asserts geometry across map, allocate, idle, and five timeouts
(`PLACE_RETRY_MS`). If the WM disagrees with the requested geometry, the two
sides can push against each other indefinitely.

### Why it is still only a suspect

Nothing here has been observed. GTK collapses many allocation cycles by
itself, and on a WM that accepts the geometry the first time the retries are
harmless. This theory predicts a freeze *correlated with opening the
navigator* — if your freezes happen while you are just typing, with the
navigator closed, this is the wrong tree.

### Confirming evidence

- `place.round_finished calls=<large>` at WARNING. A healthy round is under 20
  (five retries, a map, a couple of allocations). Hundreds means a fight.
- `place.reentrant_skipped` appearing at DEBUG in bulk — the guard is catching
  recursion that would otherwise have nested.
- Stall timestamps clustering on navigator opens.

### Refuting evidence

- Stalls with no `place.*` events nearby.
- `place.round_finished` consistently in single digits.

### If confirmed

Do not start by deleting the retries — they exist because a single `move()`
was being ignored. In rough order of preference:

1. Disconnect `size-allocate` after the first non-degenerate allocation, rather
   than re-placing on every one. The handler already tries this
   (`placement.py:150`) but calls `place()` *before* the check.
2. Make `_force()` a no-op when the window is already at the requested
   geometry. This breaks the cycle at the source: no resize, no allocation.
3. Drop `set_geometry_hints()` — pinning min/max/base size on every placement
   is the most likely thing to make a WM argue.
4. Only then consider reducing `PLACE_RETRY_MS`.

The re-entrancy guard already in `place()` prevents nesting, but not a
ping-pong across separate main-loop iterations. It was added because it cannot
change placement behaviour; the fixes above can, which is why they need
evidence first.

---

## Suspect 2 — the 1.5s hook tick

**Partly addressed.** `magere_brug/plugin.py:397`

### Mechanism

`_hook_terminal_listeners()` runs every `HOOK_INTERVAL_MS` (1.5s) forever, on
the main thread. It iterates every terminal, iterates every toplevel, and
refreshes the keybinding cache.

The expensive part was the keybinding refresh: it called `Config()`, and
Terminator's `Config.__init__` runs `connect_gsetting_callbacks()` — two
`Gio.Settings.new()` calls plus three signal connects, **every time**. Each
instance then holds live dconf watches inside a reference cycle
(`Settings → bound method → Config → Settings`) until the cyclic collector
runs. Twice a second, for the life of the process.

This one is confirmed by reading Terminator's source, not merely suspected:
`terminatorlib/config.py:298` and `:385`.

### Status

Fixed — the `Config` instance is now cached
(commit `perf: build Terminator's Config once instead of twice a second`).
Freshness is unaffected: `ConfigBase` is a Borg whose `load()` short-circuits,
so every instance always read the same in-memory tree.

It is listed here because **it is not proven to have been the freeze.** It was
an unnecessary cost with a plausible mechanism. The fix is a separate commit
specifically so `hook.tick` spans on either side can be compared.

### Confirming evidence

- `hook.tick.slow` at all. The tick should now be sub-millisecond.
- `process.rss` (DEBUG) climbing steadily across a long session would suggest
  something in this path is still accumulating.

---

## Suspect 3 — `/proc` reads on the open path

**Minor.** `magere_brug/plugin.py:637`, `magere_brug/util.py:26`

`_gather_terminals()` calls `cmdline_for_pid()` per terminal, which opens and
reads `/proc/<pid>/cmdline`. With a handful of terminals this is microseconds.
With many terminals, or on a loaded machine where `/proc` reads block, it is on
the critical path of opening the navigator.

Predicts: a navigator that is *slow to open*, not a freeze while typing.

- **Confirming:** `gather_terminals.slow`, `navigator.open.slow`.
- **If confirmed:** cache per PID — a process's cmdline rarely changes.

---

## Suspect 4 — not yet instrumented

Honest list of things that could stall the loop and would currently show up as
a bare `mainloop.stall` with no attribution:

- **`_on_vte_key_press` (`plugin.py:517`)** runs on *every keystroke in every
  terminal*. It is cheap now (cached accel comparison) and returns early, but
  it is not spanned — spanning every keystroke would cost more than it
  measures. If stalls correlate with typing rather than with the navigator,
  this is where to look first.
- **`_note_mru` (`plugin.py:500`)** rebuilds the MRU list on every focus
  change. It is O(terminals) and fine, unless the list is somehow growing.
  `process.rss` logs `mru=` at DEBUG for exactly this reason.
- **X11 round trips.** `get_position()`, `get_origin()`, `get_monitor_at_*` all
  talk to the X server. A slow or busy server turns these into blocking calls,
  and the stall would be attributed to whichever span happens to contain them.
- **Terminator and VTE themselves.** This plugin is not the only thing on that
  main loop. A stall with no magereBrug event nearby is evidence *against* this
  plugin, and worth taking as such.

---

## What this cannot see

- **A segfault.** No Python runs after it. The only trace is a `session.start`
  with no `session.end`; use `coredumpctl list terminator` or `journalctl` for
  the crash itself.
- **Anything before plugin setup.** Logging starts in `_deferred_setup`, on the
  first idle callback after construction.
- **`SIGKILL`, and `SIGTERM`.** Neither runs `atexit`, so both look identical
  to a crash in the log. Read a missing end marker as "did not exit cleanly".
- **Blocking inside GTK/VTE C code.** The watchdog will report the stall, but
  no Python span will contain it, so there will be nothing to attribute it to.
  That absence is itself informative — see Suspect 4.

## Notes for whoever picks this up

The two behaviour changes shipped alongside the instrumentation were chosen
because they can only reduce work and cannot alter observable behaviour:

- the re-entrancy guard in `place()`
- coalescing the window `'add'` signal into one pending rescan (it previously
  queued an independent 100ms full hook pass per widget added, so one split
  produced a burst)

Everything else was left alone on purpose. Changing the code and the
measurement at the same time is how you end up unable to tell which one helped.
