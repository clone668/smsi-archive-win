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
QPushButton#primaryButton {
    color: #ffffff;
    background: #0f6cbd;
    border-color: #0f6cbd;
}
QPushButton#primaryButton:hover { background: #115ea3; border-color: #115ea3; }
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
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 2px solid #0f6cbd; }
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
QToolTip { background: #2c2c30; color: #ffffff; border: none; padding: 5px; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #c5c5ca; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
