"""Themes: a palette, a set of layout metrics, and the stylesheet built from them.

A theme here is not a colour scheme with a name.  It owns the numbers the window
is laid out from as well - control heights, row heights, card padding, how many
readings fit in a row of the overview strip, whether a form puts its label beside
the field or above it - because those are what decide whether a window reads as
dense and administrative or as something you can check from across the room.

Deliberately free of Qt imports: the configuration module normalises the stored
theme name, and it is also read by the alerting worker and the sync engine, which
must keep running in a process that never builds a widget.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from string import Template
from typing import Any


@dataclass(frozen=True)
class Palette:
    """Every colour the stylesheet names.

    Spelled out rather than derived from a few seeds: the light theme's shades
    were chosen by eye against real content, and a "smart" derivation would
    quietly move all of them the first time someone edits a seed.
    """

    # surfaces
    window: str = "#f5f6f7"
    surface: str = "#ffffff"
    surface_alt: str = "#fafbfc"
    sunken: str = "#f6f6f7"
    border: str = "#dfe1e5"
    divider: str = "#e3e5e8"
    # text
    text: str = "#202124"
    heading: str = "#18181b"
    section: str = "#27272a"
    muted: str = "#6b6b72"
    soft: str = "#5f6368"
    # accent
    accent: str = "#0f6cbd"
    accent_hover: str = "#115ea3"
    accent_ring: str = "#9dc7ea"
    accent_soft: str = "#e7f1fa"
    accent_faint: str = "#e9eef3"
    on_accent: str = "#ffffff"
    # buttons
    button: str = "#ffffff"
    button_text: str = "#252529"
    button_border: str = "#d2d2d6"
    button_hover: str = "#f3f3f4"
    button_hover_border: str = "#b9b9be"
    button_pressed: str = "#e8e8ea"
    disabled_text: str = "#a1a1a7"
    disabled_surface: str = "#f2f2f3"
    disabled_border: str = "#e2e2e4"
    tab_text: str = "#4b4d52"
    tab_hover: str = "#f0f2f4"
    # inputs
    field: str = "#ffffff"
    field_border: str = "#cfcfd4"
    field_readonly: str = "#f1f1f3"
    field_readonly_text: str = "#5d5d64"
    spinner: str = "#f4f5f7"
    spinner_border: str = "#dfe0e4"
    spinner_hover: str = "#e7eaee"
    spinner_pressed: str = "#dbdee3"
    placeholder: str = "#9a9aa2"
    # tree
    row_hover: str = "#f4f7fa"
    row_selected: str = "#e6f2fb"
    row_line: str = "#eff0f2"
    header_text: str = "#4f5156"
    header_sep: str = "#e2e2e5"
    header_line: str = "#dcdce0"
    # progress
    track: str = "#e7e7ea"
    tree_track: str = "#e8eaed"
    tree_fill: str = "#2b78c5"
    tree_fill_done: str = "#2b78c5"
    tree_text: str = "#3f4247"
    tree_text_on_fill: str = "#ffffff"
    # status
    success: str = "#157347"
    warning: str = "#9a6700"
    danger: str = "#b42318"
    icon: str = "#3f3f46"
    # An icon is a thin stroke rather than a block of text, so the green that
    # reads correctly inside a 16px glyph is not the green that reads correctly
    # as a word.  Two tokens, both deliberate.
    icon_success: str = "#18825d"
    # chrome
    tooltip: str = "#2c2c30"
    tooltip_text: str = "#ffffff"
    scrollbar: str = "#c5c5ca"
    menu: str = "#ffffff"
    menu_text: str = "#202124"
    menu_border: str = "#dfe1e5"
    menu_hover: str = "#e6f2fb"
    # floating status block
    float_stop0: str = "rgba(250, 253, 255, 238)"
    float_stop1: str = "rgba(238, 247, 252, 232)"
    float_stop2: str = "rgba(226, 239, 248, 226)"
    float_border: str = "rgba(128, 151, 170, 135)"
    float_title: str = "#1f2933"
    float_meta: str = "#52606d"
    float_track: str = "rgba(86, 111, 130, 48)"


@dataclass(frozen=True)
class Metrics:
    """The numbers the layout is built from.

    Two kinds live here on purpose.  The first half is spent inside the
    stylesheet; the second half is spent by the window's own layout code, which
    is why a theme can change the shape of a page and not only its shading.
    """

    # type
    font_pt: int = 10
    brand_pt: int = 14
    caption_pt: int = 9
    page_title_pt: int = 15
    section_pt: int = 11
    metric_value_pt: int = 13
    metric_label_pt: int = 9
    tree_progress_pt: int = 8
    float_title_pt: int = 10
    float_meta_pt: int = 9
    # controls
    control_height: int = 34
    button_padding: int = 14
    field_padding: int = 9
    button_radius: int = 6
    field_radius: int = 5
    card_radius: int = 6
    bar_radius: int = 3
    tab_radius: int = 5
    tab_min_width: int = 104
    tab_height: int = 36
    tab_padding: int = 13
    toolbar_min_width: int = 82
    # tree
    row_height: int = 30
    header_height: int = 31
    tree_icon: int = 18
    tree_min_section: int = 64
    tree_columns: tuple[tuple[int, int], ...] = (
        (1, 86), (2, 90), (3, 90), (4, 70), (5, 122), (6, 220),
    )
    # bars
    progress_height: int = 7
    tree_progress_height: int = 16
    float_progress_height: int = 4
    float_radius: int = 10
    float_size: tuple[int, int] = (164, 72)
    scrollbar_width: int = 12
    # window chrome (the top bar, not the tree header)
    top_bar_height: int = 62
    top_bar_margins: tuple[int, int, int, int] = (20, 0, 18, 0)
    top_bar_spacing: int = 6
    top_bar_gap: int = 24
    # pages
    page_margins: tuple[int, int, int, int] = (20, 16, 20, 14)
    page_spacing: int = 12
    # overview strip
    summary_margins: tuple[int, int, int, int] = (14, 11, 14, 11)
    summary_spacing: int = 12
    summary_row_spacing: int = 10
    summary_columns: int = 8
    summary_dividers: bool = True
    summary_divider_height: int = 42
    metric_gap: int = 1
    metric_min_widths: tuple[int, ...] = (104, 76, 100, 100, 88, 82, 72, 104)
    # task strip
    task_margins: tuple[int, int, int, int] = (14, 9, 14, 9)
    task_spacing: int = 6
    cancel_width: int = 82
    # settings
    content_max_width: int = 840
    card_margins: tuple[int, int, int, int] = (18, 14, 18, 16)
    card_spacing: int = 10
    form_h_spacing: int = 18
    form_v_spacing: int = 9
    form_wrap_rows: bool = False
    label_align_right: bool = True
    number_width: int = 126
    port_width: int = 106
    choice_width: int = 160
    browse_width: int = 88


@dataclass(frozen=True)
class Theme:
    """A named pairing of a palette and a set of metrics."""

    name: str
    label: str
    caption: str
    palette: Palette = field(default_factory=Palette)
    metrics: Metrics = field(default_factory=Metrics)
    dark: bool = False


_QSS = Template(r"""
QWidget {
    color: $text;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: ${font_pt}pt;
}
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {
    background: $window;
}
QFrame#appHeader {
    background: $surface;
    border-bottom: 1px solid $border;
}
QLabel#brandTitle {
    background: transparent;
    font-size: ${brand_pt}pt;
    font-weight: 600;
    color: $heading;
}
QLabel#brandCaption, QLabel#muted, QLabel#sectionDescription, QLabel#versionLabel {
    color: $muted;
}
QLabel#versionLabel {
    background: transparent;
    font-size: ${caption_pt}pt;
}
QLabel#inlineLabel { color: $soft; }
QLabel#pageTitle {
    font-size: ${page_title_pt}pt;
    font-weight: 600;
    color: $heading;
}
QLabel#sectionTitle {
    font-size: ${section_pt}pt;
    font-weight: 600;
    color: $section;
}
QLabel#metricValue {
    font-size: ${metric_value_pt}pt;
    font-weight: 600;
    color: $heading;
}
QLabel#metricLabel {
    color: $muted;
    font-size: ${metric_label_pt}pt;
}
QPushButton {
    min-height: ${control_height}px;
    padding: 0 ${button_padding}px;
    border: 1px solid $button_border;
    border-radius: ${button_radius}px;
    background: $button;
    color: $button_text;
}
QPushButton:hover { background: $button_hover; border-color: $button_hover_border; }
QPushButton:pressed { background: $button_pressed; }
QPushButton:disabled { color: $disabled_text; background: $disabled_surface; border-color: $disabled_border; }
/* A fully painted button hides the platform focus rectangle, so without this
   rule keyboard focus is invisible on every button in the window.  The padding
   and min-height give back exactly what the second border pixel takes, so a
   focused button is the same size as an unfocused one and the toolbar does not
   shift when tabbing - see build_qss for where those two numbers come from. */
QPushButton:focus { border: 2px solid $accent; padding: 0 ${button_focus_padding}px; min-height: ${focus_height}px; }
QPushButton#primaryButton {
    color: $on_accent;
    background: $accent;
    border-color: $accent;
}
QPushButton#primaryButton:hover { background: $accent_hover; border-color: $accent_hover; }
/* An accent ring on an accent fill is not a ring.  An id selector also outranks
   QPushButton:focus, so the filled buttons have to name their own focus colour. */
QPushButton#primaryButton:focus { border-color: $accent_ring; }
QPushButton#dangerButton { color: $danger; }
QPushButton#topTab {
    min-width: ${tab_min_width}px;
    min-height: ${tab_height}px;
    max-height: ${tab_height}px;
    padding: 0 ${tab_padding}px;
    border: none;
    border-radius: ${tab_radius}px;
    background: transparent;
    color: $tab_text;
}
/* The tab has a capped height, so a focus border would eat its text instead of
   growing the button.  Kept before :hover and :checked so those still win. */
QPushButton#topTab:focus { background: $accent_faint; color: $accent; }
QPushButton#topTab:hover { background: $tab_hover; }
QPushButton#topTab:checked {
    background: $accent_soft;
    color: $accent;
    font-weight: 600;
}
QPushButton#toolbarButton { min-width: ${toolbar_min_width}px; }
QFrame#summaryBar, QFrame#taskBar, QFrame#settingsSection {
    background: $surface;
    border: 1px solid $border;
    border-radius: ${card_radius}px;
}
QFrame#metricDivider { background: $divider; }
QTreeWidget {
    background: $surface;
    alternate-background-color: $surface_alt;
    border: 1px solid $border;
    border-radius: ${card_radius}px;
    outline: none;
    selection-background-color: $row_selected;
    selection-color: $text;
}
QTreeWidget::item { min-height: ${row_height}px; border-bottom: 1px solid $row_line; }
QTreeWidget::item:hover { background: $row_hover; }
QTreeWidget::item:selected { background: $row_selected; }
QHeaderView::section {
    background: $sunken;
    color: $header_text;
    border: none;
    border-right: 1px solid $header_sep;
    border-bottom: 1px solid $header_line;
    min-height: ${header_height}px;
    padding: 0 8px;
    font-weight: 600;
}
QLineEdit, QSpinBox, QComboBox {
    min-height: ${control_height}px;
    background: $field;
    border: 1px solid $field_border;
    border-radius: ${field_radius}px;
    padding: 0 ${field_padding}px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
/* A spin box asks for 3px more than the text field beside it, and it keeps
   asking no matter what the arrow buttons below are given - the extra comes
   from the spin box's own size hint, not from the buttons.  Capping the content
   box at the same height the other two fields use settles the row. */
QSpinBox { max-height: ${control_height}px; }
/* Giving the box a border and a radius without also styling the two arrow
   buttons leaves the spinner drawn by the unstyled fallback: a stub in the
   corner with clipped arrows.  Sized from the border edge so the pair fills the
   box exactly (up + down == content height + both border pixels). */
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border;
    width: 19px;
    background: $spinner;
    border-left: 1px solid $spinner_border;
}
QSpinBox::up-button {
    subcontrol-position: top right;
    height: ${spin_up_height}px;
    border-top-right-radius: ${inner_radius}px;
}
QSpinBox::down-button {
    subcontrol-position: bottom right;
    height: ${spin_down_height}px;
    border-bottom-right-radius: ${inner_radius}px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: $spinner_hover; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: $spinner_pressed; }
QSpinBox::up-arrow, QSpinBox::down-arrow { width: 9px; height: 9px; }
QComboBox::drop-down {
    subcontrol-origin: border;
    subcontrol-position: center right;
    width: 26px;
    border-left: 1px solid $spinner_border;
    border-top-right-radius: ${inner_radius}px;
    border-bottom-right-radius: ${inner_radius}px;
    background: $spinner;
}
QComboBox::down-arrow { width: 10px; height: 10px; }
QComboBox QAbstractItemView {
    background: $field;
    border: 1px solid $field_border;
    selection-background-color: $row_selected;
    selection-color: $text;
    outline: none;
}
/* Two accent pixels of focus without moving anything: the content box gives
   back the pixel the border takes, so the text does not step sideways and the
   form row does not grow when a field is focused. */
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 2px solid $accent;
    min-height: ${focus_height}px;
    padding: 0 ${field_focus_padding}px;
}
QLineEdit:read-only { background: $field_readonly; color: $field_readonly_text; }
QLineEdit { placeholder-text-color: $placeholder; }
QProgressBar {
    min-height: ${progress_height}px;
    max-height: ${progress_height}px;
    border: none;
    border-radius: ${bar_radius}px;
    background: $track;
    text-align: center;
}
QProgressBar::chunk { background: $accent; border-radius: ${bar_radius}px; }
QProgressBar#treeProgress {
    min-height: ${tree_progress_height}px;
    max-height: ${tree_progress_height}px;
    border: none;
    border-radius: ${bar_radius}px;
    background: $tree_track;
}
QProgressBar#treeProgress::chunk {
    background: $tree_fill;
    border-radius: ${bar_radius}px;
}
QProgressBar#treeProgress[progressState="verified"]::chunk { background: $tree_fill_done; }
QLabel#treeProgressText {
    background: transparent;
    color: $tree_text;
    font-size: ${tree_progress_pt}pt;
    font-weight: 600;
}
QLabel#treeProgressText[onFill="true"] { color: $tree_text_on_fill; }
QFrame#floatingPanel {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 $float_stop0,
        stop: 0.55 $float_stop1,
        stop: 1 $float_stop2
    );
    border: 1px solid $float_border;
    border-radius: ${float_radius}px;
}
QLabel#floatingTitle {
    background: transparent;
    color: $float_title;
    font-size: ${float_title_pt}pt;
    font-weight: 600;
}
QLabel#floatingMeta {
    background: transparent;
    color: $float_meta;
    font-size: ${float_meta_pt}pt;
}
QFrame#floatingPanel QProgressBar {
    min-height: ${float_progress_height}px;
    max-height: ${float_progress_height}px;
    border-radius: ${float_bar_radius}px;
    background: $float_track;
}
QFrame#floatingPanel QProgressBar::chunk { border-radius: ${float_bar_radius}px; }
QStatusBar { background: $surface; color: $soft; border-top: 1px solid $border; }
QStatusBar::item { border: none; }
QLabel#statusChip { color: $soft; }
QLabel#statusChip[state="on"] { color: $success; font-weight: 600; }
QToolTip {
    background: $tooltip;
    color: $tooltip_text;
    border: none;
    padding: 5px;
}
/* The tray menu is a top-level window of its own.  It inherits nothing from the
   main window, so a theme that does not name it stays light while everything
   else goes dark. */
QMenu {
    background: $menu;
    color: $menu_text;
    border: 1px solid $menu_border;
    padding: 4px;
}
QMenu::item { padding: 6px 22px 6px 26px; border-radius: 4px; }
QMenu::item:selected { background: $menu_hover; color: $menu_text; }
QMenu::item:disabled { color: $disabled_text; }
QMenu::separator { height: 1px; background: $menu_border; margin: 4px 8px; }
QScrollBar:vertical { background: transparent; width: ${scrollbar_width}px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
/* The file tree can scroll sideways.  Without these the horizontal bar falls
   back to the Fusion default and looks like it belongs to another program. */
QScrollBar:horizontal { background: transparent; height: ${scrollbar_width}px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scrollbar; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""")


def build_qss(theme: Theme) -> str:
    """Render the stylesheet for one theme.

    The derived numbers are computed here rather than stored, because each one
    is a relationship that has to hold no matter what a theme asks for:
    * a focused control gives back the pixel its second border takes, in both
      directions, so focus never moves anything;
    * the two spin buttons together fill the box from border to border;
    * the inner corner sits one pixel inside the outer one.
    A theme that only sets ``control_height`` gets all three for free.
    """

    values: dict[str, Any] = {**asdict(theme.palette), **asdict(theme.metrics)}
    metrics = theme.metrics
    values.update(
        focus_height=metrics.control_height - 2,
        button_focus_padding=metrics.button_padding - 1,
        field_focus_padding=metrics.field_padding - 1,
        inner_radius=max(metrics.field_radius - 1, 0),
        spin_up_height=(metrics.control_height + 3) // 2,
        spin_down_height=(metrics.control_height + 2) // 2,
        float_bar_radius=metrics.float_progress_height // 2,
    )
    return _QSS.substitute(values)


STANDARD_THEME = Theme(
    name="standard",
    label="标准",
    caption="浅色紧凑排版，一屏放下更多归档日期。",
)

# 值守: for the machine this client actually runs on - a small box in a corner,
# window minimised, nobody sitting in front of it.  When someone does look, they
# look from a few steps away and they are asking one question: is it still
# syncing.  So the readings get large, the rows get taller than a mouse needs
# them to be, the overview strip stops being a single thin row of eight small
# numbers and becomes a 4x2 board, and the settings forms put their labels above
# their fields so the page stays legible at a narrower width.
WATCH_THEME = Theme(
    name="watch",
    label="值守",
    caption="深色值守面板，读数更大，概览排成两行，适合远处看一眼。",
    dark=True,
    palette=Palette(
        window="#12161c",
        surface="#1a2029",
        surface_alt="#1e252f",
        sunken="#151b23",
        border="#2b3541",
        divider="#2b3541",
        text="#e6ebf2",
        heading="#f4f7fb",
        section="#dbe3ec",
        muted="#94a3b4",
        soft="#a7b4c4",
        accent="#4da3ff",
        accent_hover="#6cb4ff",
        accent_ring="#9ccbff",
        accent_soft="#1b3a5c",
        accent_faint="#17293c",
        on_accent="#08121d",
        button="#232b36",
        button_text="#e6ebf2",
        button_border="#38434f",
        button_hover="#2b3542",
        button_hover_border="#4a586a",
        button_pressed="#1c242e",
        disabled_text="#5c6875",
        disabled_surface="#1b222b",
        disabled_border="#262f39",
        tab_text="#a7b4c4",
        tab_hover="#222b36",
        field="#10151c",
        field_border="#38434f",
        field_readonly="#171d25",
        field_readonly_text="#93a0af",
        spinner="#222b36",
        spinner_border="#333e4a",
        spinner_hover="#2c3846",
        spinner_pressed="#1a222b",
        placeholder="#6b7887",
        row_hover="#212a35",
        row_selected="#1e3752",
        row_line="#232c37",
        header_text="#9fadbc",
        header_sep="#2b3541",
        header_line="#313c48",
        track="#232c37",
        tree_track="#232c37",
        tree_fill="#4da3ff",
        # The one place the dark theme says something the light one cannot: a
        # finished bar turns green, so a glance at the tree tells you which
        # dates are done without reading a single percentage.
        tree_fill_done="#37b48b",
        tree_text="#cdd8e4",
        tree_text_on_fill="#08121d",
        success="#46c48b",
        warning="#e0b23c",
        danger="#ff6b5e",
        icon="#b7c3d1",
        icon_success="#46c48b",
        tooltip="#2c3644",
        tooltip_text="#eef3f9",
        scrollbar="#3a4552",
        menu="#1a2029",
        menu_text="#e6ebf2",
        menu_border="#2b3541",
        menu_hover="#1e3752",
        float_stop0="rgba(24, 31, 40, 240)",
        float_stop1="rgba(20, 27, 35, 236)",
        float_stop2="rgba(16, 22, 29, 232)",
        float_border="rgba(120, 150, 180, 120)",
        float_title="#eef3f9",
        float_meta="#a7b4c4",
        float_track="rgba(160, 185, 210, 55)",
    ),
    metrics=Metrics(
        font_pt=11,
        brand_pt=15,
        caption_pt=10,
        page_title_pt=16,
        section_pt=12,
        metric_value_pt=19,
        metric_label_pt=10,
        tree_progress_pt=9,
        float_title_pt=11,
        float_meta_pt=10,
        control_height=38,
        button_padding=16,
        field_padding=11,
        button_radius=8,
        field_radius=7,
        card_radius=10,
        bar_radius=4,
        tab_radius=7,
        tab_min_width=116,
        tab_height=40,
        tab_padding=15,
        toolbar_min_width=92,
        row_height=36,
        header_height=34,
        tree_icon=20,
        tree_min_section=72,
        tree_columns=((1, 96), (2, 104), (3, 104), (4, 82), (5, 132), (6, 240)),
        progress_height=9,
        tree_progress_height=18,
        float_progress_height=5,
        float_radius=12,
        float_size=(190, 80),
        scrollbar_width=13,
        top_bar_height=70,
        top_bar_margins=(22, 0, 20, 0),
        top_bar_spacing=7,
        top_bar_gap=26,
        page_margins=(22, 16, 22, 16),
        page_spacing=14,
        summary_margins=(16, 13, 16, 13),
        summary_spacing=14,
        summary_row_spacing=12,
        summary_columns=4,
        summary_dividers=False,
        summary_divider_height=48,
        metric_gap=2,
        metric_min_widths=(140, 120, 140, 140, 130, 120, 110, 140),
        task_margins=(16, 11, 16, 11),
        task_spacing=8,
        cancel_width=92,
        content_max_width=760,
        card_margins=(20, 16, 20, 18),
        card_spacing=12,
        form_h_spacing=16,
        form_v_spacing=12,
        form_wrap_rows=True,
        label_align_right=False,
        number_width=138,
        port_width=118,
        choice_width=176,
        browse_width=96,
    ),
)

THEMES: dict[str, Theme] = {theme.name: theme for theme in (STANDARD_THEME, WATCH_THEME)}
DEFAULT_THEME_NAME = STANDARD_THEME.name


def normalize_theme_name(value: Any) -> str:
    """Any stored value becomes a theme that exists.

    The configuration file is written by this program but read on the next
    launch, and a name that no longer exists - a renamed theme, a hand edit, a
    downgrade to an older build - must not be able to stop the window from
    opening.  An unknown name is simply the standard theme.
    """

    if isinstance(value, str):
        name = value.strip().lower()
        if name in THEMES:
            return name
    return DEFAULT_THEME_NAME


def resolve_theme(value: Any) -> Theme:
    """The theme for a stored name, falling back to the standard one."""

    return THEMES[normalize_theme_name(value)]
