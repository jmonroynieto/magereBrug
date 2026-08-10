# magereBrug

Quick terminal switcher for [Terminator](https://gnome-terminator.org/).

Open a searchable navigator (default **Ctrl+P** when you have two or more
terminals), jump by fuzzy title / path / command, grouped by window and ordered
by most-recently-used.

## Features

- Configurable hotkey (default `<Primary>p` / Ctrl+P)
- Only intercepts the hotkey when **more than one** terminal exists
- Grouped by Terminator window; **MRU** order within each window
- Fuzzy filter over title, window name, cwd, and command
- ↑↓ / Page navigation, **1–9** jump (empty search), Enter / Esc refocus
- Empty query + Enter → previous MRU terminal
- Click-outside / focus-loss dismisses
- Theme-aligned GTK CSS
- Hardened centering for undecorated dialogs (X11-friendly)
- Rotating log file + `diagnose` CLI — see [Logging & debugging](#logging--debugging)

## Install

This repo is meant to live in Terminator’s plugins directory:

```text
~/.config/terminator/plugins/
├── magereBrug.py          # entry point (AVAILABLE)
└── magere_brug/           # implementation package
```

### Fresh clone

```bash
mkdir -p ~/.config/terminator/plugins
git clone <this-repo-url> /tmp/magere-brug
cp -a /tmp/magere-brug/magereBrug.py /tmp/magere-brug/magere_brug \
  ~/.config/terminator/plugins/
```

Or clone directly into the plugins dir if you keep only this plugin there.

Enable the plugin in Terminator: **Right-click → Preferences → Plugins → MagereBrug**.

Restart Terminator (or reload plugins).

### Updating an existing install

Copy the **whole** `magere_brug/` directory, not just the files you think
changed. Releases add modules; an update that only overwrites existing files
leaves `plugin.py` importing something that isn't there, and Terminator then
fails to load the plugin **silently** — no error, the menu entry and hotkey
just don't appear.

```bash
rm -rf ~/.config/terminator/plugins/magere_brug
cp -a magereBrug.py magere_brug ~/.config/terminator/plugins/
```

## Configuration

Edit `~/.config/terminator/config`:

```ini
[plugins]
  [[MagereBrug]]
  keybinding = <Primary><Shift>p
```

| Key | Meaning |
|-----|---------|
| `keybinding` | GTK accelerator (also accepts `hotkey` / `accel`) |
| `debug` | `True` turns the log level up to DEBUG |
| `log_level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `WARNING`) |

Examples:

- `<Primary>p` — Ctrl+P (default)
- `<Primary><Shift>p` — Ctrl+Shift+P
- `<Alt>grave` — Alt+\`

The binding is re-read every 1.5s, so changes made through **Preferences**
apply without a restart. Hand-edits to the config *file* do need a Terminator
restart: Terminator parses the file once at startup and serves every later
read from that in-memory copy.

## Keyboard

| Key | Action |
|-----|--------|
| Hotkey | Toggle navigator (if ≥2 terminals) |
| ↑ / ↓ | Move selection |
| Page Up / Down | Jump by 5 |
| F2 or **Rename** | Rename selected terminal (inline bar) |
| 1–9 | Open Nth row (when search is empty) |
| Enter | Open selection; empty query → previous MRU; apply rename |
| Esc | Cancel rename, or close and refocus source terminal |
| Click outside | Close |

## Logging & debugging

The plugin writes to a rotating log file, so you no longer need to launch
Terminator from another terminal to see anything:

```text
~/.local/state/magereBrug/magereBrug.log     ($XDG_STATE_HOME if set)
```

Default level is **WARNING**, which in normal operation means the file stays
near-empty: a session marker at start and shutdown, plus anything that actually
went wrong. This is deliberate — the log runs on the GTK main thread, and a
chatty logger would cause the very stalls it is meant to measure.

### Reading the log

```bash
# works from anywhere
D=~/.config/terminator/plugins/magere_brug/diagnose.py

python3 $D            # summary: sessions, stalls, slow ops, errors
python3 $D --tail 50  # last 50 raw lines
python3 $D --follow   # live tail, survives rotation
python3 $D --json     # machine-readable summary
python3 $D --path     # just print the log path
```

From the plugins directory (or the repo root) the module form works too:
`python3 -m magere_brug.diagnose`.

**Right-click → magereBrug Diagnostics…** prints the log path and a one-line
health summary straight into the terminal you clicked in.

### Turning it up

```bash
MAGEREBRUG_LOG_LEVEL=DEBUG terminator     # no config edit, no restart dance
```

| Variable | Effect |
|----------|--------|
| `MAGEREBRUG_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` — overrides config |
| `MAGEREBRUG_LOG_FILE` | Write somewhere else |
| `MAGEREBRUG_LOG_FORMAT` | `logfmt` (default) or `json` for JSON Lines |
| `MAGEREBRUG_LOG_STDERR` | `1` also mirrors to stderr (the old behaviour) |
| `MAGEREBRUG_LOG_MAX_BYTES` | Rotation size, default 1 MiB × 3 backups |
| `MAGEREBRUG_SLOW_MS` | Span threshold in ms, default 50 |

### Line format

Logfmt — one event per line, greppable and awk-able. Every line carries the
PID, because one file collects every Terminator process and plugin reload:

```text
2026-08-10T12:00:00.123 WARN pid=4711 mod=placement event=place.slow duration_ms=312
```

### What to look for

| Event | Meaning |
|-------|---------|
| `mainloop.stall` | The GTK main loop was blocked. `max_ms` is how long — this is a freeze, measured. |
| `place.slow` | Dialog centering took too long. High `calls` in `place.round_finished` means the WM is rejecting our geometry and we are re-asserting it in a loop. |
| `hook.tick.slow` | The 1.5s background rescan is expensive. |
| `gather_terminals.slow` | Building the navigator list is slow (it reads `/proc` per terminal). |
| `navigator.open.slow` | Opening the palette is slow — check the spans inside it. |
| `place.round_finished` at WARN | More than 20 `place()` calls in one round — the placement controller and the window manager are fighting. |
| `session.start` with no matching `session.end` | The process did not exit cleanly. `diagnose` calls this out; see the note below. |

A GTK/VTE **segfault** leaves nothing in this log — no Python runs after it.
The missing `session.end` marker is the only evidence, which is why it exists.
For a segfault itself, check `coredumpctl list terminator` or `journalctl`.

The end marker is written from `atexit`, which runs on a clean interpreter
exit and does not run on a segfault or `SIGKILL`. It is deliberately not tied
to the plugin's `unload()`: Terminator calls that only when a plugin is
reloaded or disabled from Preferences, never on quit. A `SIGTERM` also skips
`atexit`, so read a missing marker as "did not exit cleanly" rather than
"segfaulted".

**Caveat:** two Terminator processes write to one file, and rotation between
them can race — one may keep writing to the renamed file. Lines carry the PID
so you can still separate them; raise `MAGEREBRUG_LOG_MAX_BYTES` if rotation
is happening often enough to matter.

## Development

```bash
# from repo root
python3 -m unittest discover -s tests -v
```

Pure helpers under `magere_brug/util.py` are covered without GTK/Terminator.
The plugin module needs Terminator + a display at runtime.

### Layout

```text
magereBrug.py              Terminator entry (AVAILABLE)
magere_brug/
  __init__.py              version
  constants.py             defaults & sizes
  css.py                   theme CSS
  util.py                  fuzzy, paths, cmdline
  log.py                   rotating log, spans, stall detector
  diagnose.py              log reader / report CLI
  placement.py             dialog centering
  plugin.py                MagereBrug class
tests/
  test_util.py
  test_log.py
  test_diagnose.py
  test_plugin_import.py
```

`util.py`, `log.py` and `diagnose.py` import neither `gi` nor `terminatorlib`,
so they run and test outside Terminator. `test_plugin_import.py` imports
`plugin.py` against stubs to catch the failure mode where Terminator swallows a
broken plugin and loads nothing at all.

## License

MIT — see [LICENSE](LICENSE).
