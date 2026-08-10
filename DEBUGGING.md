# Debugging magereBrug freezes and crashes

Working notes on why this plugin might be stalling Terminator, what evidence
would settle each theory, and what the instrumentation can and cannot see.

**Status:** the suspects below are derived from reading the code and
Terminator's source. Neither has been observed causing a freeze. That is what
the logging is for — this document exists so that the next freeze produces an
answer instead of another round of guessing.

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
