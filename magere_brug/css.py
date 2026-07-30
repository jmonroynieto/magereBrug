"""GTK CSS for the navigator (tracks theme symbolic colours)."""

NAV_CSS = b"""
window.magere-brug-nav {
  background-color: @theme_bg_color;
  color: @theme_fg_color;
  border: 1px solid @borders;
  border-radius: 10px;
}
window.magere-brug-nav entry,
window.magere-brug-nav search {
  background-color: @theme_base_color;
  color: @theme_text_color;
  border-radius: 6px;
}
.magere-brug-muted {
  color: alpha(currentColor, 0.62);
  font-size: 0.92em;
}
.magere-brug-hint {
  color: alpha(currentColor, 0.55);
  font-size: 0.85em;
}
.magere-brug-header {
  font-weight: bold;
  font-size: 0.8em;
  opacity: 0.75;
  padding: 4px 0 2px 0;
}
.magere-brug-current-title {
  font-weight: bold;
}
.magere-brug-empty {
  color: alpha(currentColor, 0.6);
  font-style: italic;
}
.magere-brug-row-index {
  color: alpha(currentColor, 0.45);
  font-family: monospace;
  font-size: 0.85em;
  min-width: 1.6em;
}
.magere-brug-actions button {
  padding: 4px 10px;
}
.magere-brug-rename-bar {
  background-color: alpha(@theme_selected_bg_color, 0.12);
  border-radius: 6px;
  padding: 6px 8px;
}
list row:selected {
  background-color: alpha(@theme_selected_bg_color, 0.85);
  color: @theme_selected_fg_color;
  border-radius: 6px;
}
"""
