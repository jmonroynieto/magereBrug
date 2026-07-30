"""Shared constants for magereBrug."""

# Terminator loads classes listed in the entry module's AVAILABLE.
PLUGIN_NAME = 'MagereBrug'

# Default accelerator. Override in Terminator config:
#
#   [plugins]
#     [[MagereBrug]]
#     keybinding = <Primary><Shift>p
#
# GTK syntax: <Primary>p, <Primary><Shift>p, <Alt>grave, …
# <Primary> is Ctrl on Linux.
DEFAULT_KEYBINDING = '<Primary>p'

NAV_WIDTH = 650
NAV_HEIGHT = 450

# Re-assert placement after map; WMs often nudge undecorated dialogs.
PLACE_RETRY_MS = (0, 16, 50, 100, 250)

# Safety-net interval for late-created terminals / splits.
HOOK_INTERVAL_MS = 1500

# Delay before focus-out can dismiss the navigator (avoids open-race).
FOCUS_OUT_ARM_MS = 200

HINT_TEXT = '↑↓ select   F2 rename   1–9 jump   Enter open   Esc close'

# Word-boundary characters for fuzzy scoring.
WORD_BOUNDARY = ' /.-_[]'
