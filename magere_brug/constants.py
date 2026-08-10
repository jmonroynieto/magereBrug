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

# Cap on a user-typed custom title (rename bar); guards against pasted
# giant strings blowing out row width or embedding control characters.
RENAME_MAX_LEN = 80

# --- diagnostics -----------------------------------------------------------

# Heartbeat for the main-loop watchdog. A timeout that should fire this often
# but fires later means the loop was blocked in between — i.e. a freeze. Short
# enough to resolve a hitch, long enough to be free (see log.StallDetector).
WATCHDOG_INTERVAL_MS = 500

# Overshoot beyond WATCHDOG_INTERVAL_MS before a tick counts as a stall.
# Below this, ordinary scheduling jitter dominates.
WATCHDOG_THRESHOLD_MS = 200

# A persistent stall pattern is aggregated into one line per this window
# rather than one line per tick.
WATCHDOG_SUMMARY_MS = 30000

# Log resident-set size every Nth watchdog tick, to test leak hypotheses
# without paying for a /proc read twice a second.
RSS_SAMPLE_EVERY = 120

# Word-boundary characters for fuzzy scoring.
WORD_BOUNDARY = ' /.-_[]'
