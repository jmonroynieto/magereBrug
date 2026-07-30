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

Examples:

- `<Primary>p` — Ctrl+P (default)
- `<Primary><Shift>p` — Ctrl+Shift+P
- `<Alt>grave` — Alt+\`

The binding is re-read on each keypress (no restart needed for the string).

## Keyboard

| Key | Action |
|-----|--------|
| Hotkey | Toggle navigator (if ≥2 terminals) |
| ↑ / ↓ | Move selection |
| Page Up / Down | Jump by 5 |
| 1–9 | Open Nth row (when search is empty) |
| Enter | Open selection; empty query → previous MRU |
| Esc | Close and refocus source terminal |
| Click outside | Close |

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
  placement.py             dialog centering
  plugin.py                MagereBrug class
tests/
  test_util.py
```

## License

MIT — see [LICENSE](LICENSE).
