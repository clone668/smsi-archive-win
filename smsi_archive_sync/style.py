APP_STYLE = r"""
QWidget {
    color: #202124;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 10pt;
}
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #f5f6f7;
}
QFrame#appHeader {
    background: #ffffff;
    border-bottom: 1px solid #dfe1e5;
}
QLabel#brandTitle {
    background: transparent;
    font-size: 14pt;
    font-weight: 600;
    color: #18181b;
}
QLabel#brandCaption, QLabel#muted, QLabel#sectionDescription, QLabel#versionLabel {
    color: #6b6b72;
}
QLabel#versionLabel {
    background: transparent;
    font-size: 9pt;
}
QLabel#inlineLabel { color: #5f6368; }
QLabel#pageTitle {
    font-size: 15pt;
    font-weight: 600;
    color: #18181b;
}
QLabel#sectionTitle {
    font-size: 11pt;
    font-weight: 600;
    color: #27272a;
}
QLabel#metricValue {
    font-size: 13pt;
    font-weight: 600;
    color: #18181b;
}
QLabel#metricLabel {
    color: #6b6b72;
    font-size: 9pt;
}
QPushButton {
    min-height: 34px;
    padding: 0 14px;
    border: 1px solid #d2d2d6;
    border-radius: 6px;
    background: #ffffff;
    color: #252529;
}
QPushButton:hover { background: #f3f3f4; border-color: #b9b9be; }
QPushButton:pressed { background: #e8e8ea; }
QPushButton:disabled { color: #a1a1a7; background: #f2f2f3; border-color: #e2e2e4; }
/* A fully painted button hides the platform focus rectangle, so without this
   rule keyboard focus is invisible on every button in the window.  The padding
   and min-height give back exactly what the second border pixel takes
   (32 + 2 + 2 == 34 + 1 + 1, 13 + 2 == 14 + 1), so a focused button is the same
   size as an unfocused one and the toolbar does not shift when tabbing. */
QPushButton:focus { border: 2px solid #0f6cbd; padding: 0 13px; min-height: 32px; }
QPushButton#primaryButton {
    color: #ffffff;
    background: #0f6cbd;
    border-color: #0f6cbd;
}
QPushButton#primaryButton:hover { background: #115ea3; border-color: #115ea3; }
/* An accent ring on an accent fill is not a ring.  An id selector also outranks
   QPushButton:focus, so the filled buttons have to name their own focus colour. */
QPushButton#primaryButton:focus { border-color: #9dc7ea; }
QPushButton#dangerButton { color: #b42318; }
QPushButton#topTab {
    min-width: 104px;
    min-height: 36px;
    max-height: 36px;
    padding: 0 13px;
    border: none;
    border-radius: 5px;
    background: transparent;
    color: #4b4d52;
}
/* The tab is capped at 36px tall, so a focus border would eat its text instead
   of growing the button.  Kept before :hover and :checked so those still win. */
QPushButton#topTab:focus { background: #e9eef3; color: #0f6cbd; }
QPushButton#topTab:hover { background: #f0f2f4; }
QPushButton#topTab:checked {
    background: #e7f1fa;
    color: #0f6cbd;
    font-weight: 600;
}
QPushButton#toolbarButton { min-width: 82px; }
QFrame#summaryBar, QFrame#taskBar, QFrame#settingsSection {
    background: #ffffff;
    border: 1px solid #dfe1e5;
    border-radius: 6px;
}
QFrame#metricDivider { background: #e3e5e8; }
QTreeWidget {
    background: #ffffff;
    alternate-background-color: #fafbfc;
    border: 1px solid #dfe1e5;
    border-radius: 6px;
    outline: none;
    selection-background-color: #e6f2fb;
    selection-color: #202124;
}
QTreeWidget::item { min-height: 30px; border-bottom: 1px solid #eff0f2; }
QTreeWidget::item:hover { background: #f4f7fa; }
QTreeWidget::item:selected { background: #e6f2fb; }
QHeaderView::section {
    background: #f6f6f7;
    color: #4f5156;
    border: none;
    border-right: 1px solid #e2e2e5;
    border-bottom: 1px solid #dcdce0;
    min-height: 31px;
    padding: 0 8px;
    font-weight: 600;
}
QLineEdit, QSpinBox, QComboBox {
    min-height: 34px;
    background: #ffffff;
    border: 1px solid #cfcfd4;
    border-radius: 5px;
    padding: 0 9px;
    selection-background-color: #0f6cbd;
}
/* A spin box asks for 3px more than the text field beside it, and it keeps
   asking no matter what the arrow buttons below are given - the extra comes
   from the spin box's own size hint, not from the buttons.  Capping the content
   box at the same 34px the other two fields use settles the row: 端口 and
   磁盘保留空间 are now exactly as tall as 主机地址 above them. */
QSpinBox { max-height: 34px; }
/* Giving the box a border and a radius without also styling the two arrow
   buttons leaves the spinner drawn by the unstyled fallback: a stub in the
   corner with clipped arrows, and a box 3px taller than the text fields beside
   it.  Sized from the border edge so the pair fills the box exactly
   (18 + 18 == 34 content + 2 border). */
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border;
    width: 19px;
    height: 18px;
    background: #f4f5f7;
    border-left: 1px solid #dfe0e4;
}
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 4px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 4px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #e7eaee; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: #dbdee3; }
QSpinBox::up-arrow, QSpinBox::down-arrow { width: 9px; height: 9px; }
QComboBox::drop-down {
    subcontrol-origin: border;
    subcontrol-position: center right;
    width: 26px;
    border-left: 1px solid #dfe0e4;
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
    background: #f4f5f7;
}
QComboBox::down-arrow { width: 10px; height: 10px; }
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #cfcfd4;
    selection-background-color: #e6f2fb;
    selection-color: #202124;
    outline: none;
}
/* Two accent pixels of focus without moving anything: the content box gives
   back the pixel the border takes (32 + 2 + 2 == 34 + 1 + 1, 8 + 2 == 9 + 1),
   so the text does not step sideways and the form row does not grow when a
   field is focused. */
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 2px solid #0f6cbd;
    min-height: 32px;
    padding: 0 8px;
}
QLineEdit:read-only { background: #f1f1f3; color: #5d5d64; }
QProgressBar {
    min-height: 7px;
    max-height: 7px;
    border: none;
    border-radius: 3px;
    background: #e7e7ea;
    text-align: center;
}
QProgressBar::chunk { background: #0f6cbd; border-radius: 3px; }
QProgressBar#treeProgress {
    min-height: 16px;
    max-height: 16px;
    border: none;
    border-radius: 3px;
    background: #e8eaed;
}
QProgressBar#treeProgress::chunk {
    background: #2b78c5;
    border-radius: 3px;
}
QLabel#treeProgressText {
    background: transparent;
    color: #3f4247;
    font-size: 8pt;
    font-weight: 600;
}
QLabel#treeProgressText[onFill="true"] { color: #ffffff; }
QFrame#floatingPanel {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 rgba(250, 253, 255, 238),
        stop: 0.55 rgba(238, 247, 252, 232),
        stop: 1 rgba(226, 239, 248, 226)
    );
    border: 1px solid rgba(128, 151, 170, 135);
    border-radius: 10px;
}
QLabel#floatingTitle {
    background: transparent;
    color: #1f2933;
    font-size: 10pt;
    font-weight: 600;
}
QLabel#floatingMeta {
    background: transparent;
    color: #52606d;
    font-size: 9pt;
}
QFrame#floatingPanel QProgressBar {
    min-height: 4px;
    max-height: 4px;
    border-radius: 2px;
    background: rgba(86, 111, 130, 48);
}
QFrame#floatingPanel QProgressBar::chunk { border-radius: 2px; }
QStatusBar { background: #ffffff; color: #5f6368; border-top: 1px solid #dfe1e5; }
QStatusBar::item { border: none; }
QLabel#statusChip { color: #5f6368; }
QLabel#statusChip[state="on"] { color: #157347; font-weight: 600; }
QToolTip { background: #2c2c30; color: #ffffff; border: none; padding: 5px; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #c5c5ca; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
/* The file tree can scroll sideways.  Without these the horizontal bar falls
   back to the Fusion default and looks like it belongs to another program. */
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c5c5ca; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""
