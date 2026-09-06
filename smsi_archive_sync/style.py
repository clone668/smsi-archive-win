"""Applying a theme to a live application.

The stylesheet is built in ``themes.py``, which stays free of Qt so the headless
parts of the client can read the same theme table.  This module is the Qt-facing
half: it hands the sheet to the application and, for a dark theme, replaces the
widget palette as well.

A stylesheet alone is not enough.  Qt's own dialogs - the message box on a failed
connection, the folder picker in settings, the tray balloon - are painted from
QPalette and know nothing about our object names, so a dark sheet with a light
palette produces a white dialog with white text in it.  The reverse also has to
work: switching back to the light theme restores the palette the platform style
handed us at startup, rather than our idea of what it contained.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .themes import Theme, build_qss

_BASE_PALETTE: QPalette | None = None


def build_palette(theme: Theme) -> QPalette:
    """A QPalette carrying the theme's colours, for Qt's own dialogs."""

    palette = QPalette()
    colours = theme.palette
    groups = (
        (QPalette.ColorRole.Window, colours.window),
        (QPalette.ColorRole.WindowText, colours.text),
        (QPalette.ColorRole.Base, colours.field),
        (QPalette.ColorRole.AlternateBase, colours.surface_alt),
        (QPalette.ColorRole.Text, colours.text),
        (QPalette.ColorRole.Button, colours.button),
        (QPalette.ColorRole.ButtonText, colours.button_text),
        (QPalette.ColorRole.ToolTipBase, colours.tooltip),
        (QPalette.ColorRole.ToolTipText, colours.tooltip_text),
        (QPalette.ColorRole.PlaceholderText, colours.placeholder),
        (QPalette.ColorRole.BrightText, colours.danger),
        (QPalette.ColorRole.Link, colours.accent),
        (QPalette.ColorRole.LinkVisited, colours.accent_hover),
        (QPalette.ColorRole.Highlight, colours.accent),
        (QPalette.ColorRole.HighlightedText, colours.on_accent),
    )
    for role, value in groups:
        palette.setColor(role, QColor(value))
    disabled = QPalette.ColorGroup.Disabled
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.HighlightedText,
    ):
        palette.setColor(disabled, role, QColor(colours.disabled_text))
    palette.setColor(disabled, QPalette.ColorRole.Base, QColor(colours.disabled_surface))
    palette.setColor(disabled, QPalette.ColorRole.Button, QColor(colours.disabled_surface))
    return palette


def apply_theme(app: QApplication, theme: Theme) -> None:
    """Point the whole application at one theme.

    The palette goes first: it is what the platform style consults while it
    polishes, and the stylesheet applied afterwards re-polishes every widget, so
    both halves land in one pass and nothing is left half dressed.
    """

    global _BASE_PALETTE
    if _BASE_PALETTE is None:
        _BASE_PALETTE = QPalette(app.palette())
    app.setPalette(build_palette(theme) if theme.dark else QPalette(_BASE_PALETTE))
    app.setStyleSheet(build_qss(theme))
