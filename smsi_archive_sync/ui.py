from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .alerts import AlertNotifier, AlertWorker
from .config import ALERT_LEVELS, AppConfig, ConfigStore, REMOTE_ROOT, validate_alert_token
from .icons import lucide_icon
from .models import ArchiveCatalog, ArchiveDay, RemoteObject, STATUS_LABELS
from .remote import HostKeyRequired, RemoteArchiveClient
from .scanner import scan_catalog
from .state import StateStore
from .storage import bytes_text, disk_status
from .style import apply_theme
from .sync_engine import SyncEngine
from .themes import THEMES, Theme, resolve_theme


LOGGER = logging.getLogger(__name__)
NODE_ROLE = Qt.ItemDataRole.UserRole
AUTO_SYNC_LOOKBACK_DAYS = 7
# Longest label on the settings page.  Every card measures one shared label
# column from it, so their input boxes start on the same vertical line instead
# of each card sizing a column from its own longest label.
FORM_LABEL_SAMPLE = "本地归档目录"
# Operations that actually run a task.  "startup" only disables the buttons
# while the cached catalogue loads and never drives the progress bar.
RUNNING_OPERATIONS = frozenset({"scan", "auto_scan", "sync", "auto_sync", "verify", "test"})


def duration_text(value: Any) -> str:
    if value is None:
        return "--"
    seconds = max(0, int(float(value)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def timestamp_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "--"
    try:
        observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def age_text(value: Any) -> str:
    """How long ago, not when.

    A full UTC stamp was the widest value in the summary bar - three times the
    width of every other one - and it still needed a subtraction in the reader's
    head, on a machine whose own clock runs at UTC+08, to answer the only
    question being asked of it: is this thing still scanning.  The exact stamp
    stays on the tooltip.
    """
    text = str(value or "").strip()
    if not text:
        return "--"
    try:
        observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - observed).total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86_400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86_400} 天前"


def compact_count(value: Any) -> str:
    number = max(0, int(value or 0))
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if number >= divisor:
            # Keep the trailing zero.  Stripping it printed 1.7M next to 1.58M
            # in the same column, so the digits of one magnitude stopped lining
            # up with each other.
            precision = 2 if number < divisor * 10 else 1
            return f"{number / divisor:.{precision}f}" + suffix
    return str(number)


def archive_content_text(day: ArchiveDay) -> str:
    business_rows = sum(
        obj.row_count for obj in day.objects if obj.kind == "business"
    )
    business_rows = business_rows or day.summary.ledger_record_count
    business_tables = day.summary.business_table_count or sum(
        1 for obj in day.objects if obj.kind == "business"
    )
    if business_tables or business_rows:
        return f"{business_tables:,} 业务表 · {compact_count(business_rows)} 业务记录"
    return f"{day.object_count:,} 文件 · {compact_count(day.row_count)} 归档总行数"


def archive_continuity(days: list[ArchiveDay]) -> tuple[int, int]:
    remote_dates: dict[str, set[date]] = defaultdict(set)
    verified_dates: dict[str, set[date]] = defaultdict(set)
    for day in days:
        try:
            archive_day = date.fromisoformat(day.archive_date)
        except ValueError:
            continue
        if day.remote_status != "remote_missing":
            remote_dates[day.collector_id].add(archive_day)
        if day.local_status == "verified":
            verified_dates[day.collector_id].add(archive_day)
    collectors = sorted(remote_dates)
    common_verified = (
        set.intersection(*(verified_dates.get(item, set()) for item in collectors))
        if collectors else set()
    )
    streak = 0
    if common_verified:
        current = max(common_verified)
        while current in common_verified:
            streak += 1
            current -= timedelta(days=1)
    gaps = 0
    for collector_id, observed in remote_dates.items():
        if not observed:
            continue
        current, end = min(observed), max(observed)
        while current <= end:
            if current not in observed:
                gaps += 1
            current += timedelta(days=1)
    return streak, gaps


def automatic_sync_days(
    days: list[ArchiveDay], *, today: date | None = None
) -> list[ArchiveDay]:
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(
        days=AUTO_SYNC_LOOKBACK_DAYS
    )
    eligible: list[ArchiveDay] = []
    for day in days:
        if not day.eligible:
            continue
        try:
            if date.fromisoformat(day.archive_date) < cutoff:
                continue
        except ValueError:
            continue
        eligible.append(day)
    return sorted(eligible, key=lambda item: (item.archive_date, item.collector_id))


class UiSignals(QObject):
    cached_catalog_ready = Signal(object)
    catalog_ready = Signal(object)
    operation_progress = Signal(object)
    operation_done = Signal(str, object)
    operation_error = Signal(str, str)
    host_key_required = Signal(str, str)
    alert_test_done = Signal(bool, str)


class TreeProgressWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("treeProgressCell")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QGridLayout(self)
        layout.setContentsMargins(3, 0, 3, 0)
        layout.setSpacing(0)
        self.bar = QProgressBar()
        self.bar.setObjectName("treeProgress")
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.label = QLabel("0%")
        self.label.setObjectName("treeProgressText")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.bar, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.label, 0, 0, Qt.AlignmentFlag.AlignCenter)

    def set_progress(self, percent: int, *, verified: bool) -> None:
        value = max(0, min(100, int(percent)))
        self.bar.setValue(value)
        self.label.setText(f"{value}%")
        state = "verified" if verified else "active"
        if self.bar.property("progressState") != state:
            self.bar.setProperty("progressState", state)
            self.bar.style().unpolish(self.bar)
            self.bar.style().polish(self.bar)
        on_fill = value >= 50
        if self.label.property("onFill") != on_fill:
            self.label.setProperty("onFill", on_fill)
            self.label.style().unpolish(self.label)
            self.label.style().polish(self.label)


class FloatingStatusWindow(QWidget):
    open_requested = Signal()
    menu_requested = Signal(QPoint)

    def __init__(self, icon: QIcon, theme: Theme) -> None:
        super().__init__(None)
        self.setObjectName("floatingWindow")
        self.setWindowTitle("SMSI 同步状态")
        self.setWindowIcon(icon)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(*theme.metrics.float_size)
        self._drag_origin: QPoint | None = None
        self._position_initialized = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.panel = QFrame()
        self.panel.setObjectName("floatingPanel")
        self.panel.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.panel.customContextMenuRequested.connect(
            lambda position: self.menu_requested.emit(self.panel.mapToGlobal(position))
        )
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(11, 9, 11, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(7)
        self.state_label = QLabel("空闲")
        self.state_label.setObjectName("floatingTitle")
        self.meta_label = QLabel("--")
        self.meta_label.setObjectName("floatingMeta")
        self.state_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self.state_label, 1)
        header.addWidget(self.meta_label)
        layout.addLayout(header)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.progress)
        outer.addWidget(self.panel)
        self.setToolTip("双击打开 SMSI 归档同步")

    def set_theme(self, theme: Theme) -> None:
        """Follow the theme's own size.

        A block whose size is pinned in pixels cannot follow a theme that makes
        its two labels a point larger: the speed reading would be the thing that
        got clipped, and that is the number the block exists for.
        """
        self.setFixedSize(*theme.metrics.float_size)

    def show_near_work_area(self) -> None:
        if not self._position_initialized:
            screen = QApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.move(area.right() - self.width() - 18, area.bottom() - self.height() - 18)
            self._position_initialized = True
        self.show()
        self.raise_()

    def set_status(
        self,
        title: str,
        detail: str,
        progress: int,
        meta: str,
        *,
        indeterminate: bool = False,
    ) -> None:
        self.state_label.setText(title)
        self.progress.setRange(0, 0 if indeterminate else 1000)
        if not indeterminate:
            self.progress.setValue(max(0, min(1000, progress)))
        self.meta_label.setText(meta)
        self.setToolTip(f"{title}\n{detail}\n{meta}\n\n双击打开主窗口")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self, config_store: ConfigStore, state_store: StateStore, icon: QIcon) -> None:
        super().__init__()
        self.config_store = config_store
        self.state_store = state_store
        self.config = self.config_store.load()
        # Before anything is built.  Control heights, row heights and point sizes
        # come out of the theme, and several of them are measured at build time -
        # the tree header fits itself to its own headings, the settings page
        # measures one shared label column - so a theme applied afterwards would
        # leave those measurements taken against the wrong font.
        self.theme = resolve_theme(self.config.theme)
        self._apply_theme_to_application()
        self.catalog = ArchiveCatalog()
        self.signals = UiSignals()
        self.busy_operation = ""
        self.cancel_event = threading.Event()
        self.icon = icon
        self._tree_day_items: dict[str, QTreeWidgetItem] = {}
        self._tree_file_items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._form_labels: list[QLabel] = []
        # Everything a theme change has to reach after the window exists.  Held
        # as references rather than looked up by object name, because a live
        # switch must move the numbers in these exact layouts, and findChildren
        # would also collect the ones a future page happens to name the same.
        self._page_layouts: list[QVBoxLayout] = []
        self._card_layouts: list[QVBoxLayout] = []
        self._forms: list[QFormLayout] = []
        self._summary_cells: list[QWidget] = []
        self._summary_boxes: list[QVBoxLayout] = []
        self._summary_dividers: list[QFrame] = []
        self._sized_inputs: list[tuple[QWidget, str]] = []
        self._theme_actions: dict[str, QAction] = {}
        self._exit_requested = False
        self._tray_notice_shown = False
        self.tray_icon: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.theme_menu: QMenu | None = None
        self.show_window_action: QAction | None = None
        self.auto_sync_action: QAction | None = None
        self.stop_sync_action: QAction | None = None
        self.floating_window_action: QAction | None = None
        self.exit_action: QAction | None = None
        self._startup_loading = True
        self.floating_window = FloatingStatusWindow(icon, self.theme)
        self.floating_window.open_requested.connect(self._show_from_tray)
        self.floating_window.menu_requested.connect(self._show_tray_menu_at)
        # The floating block is an explicit, per-session view; it never appears during startup.
        self.config.floating_window_enabled = False
        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.setInterval(15 * 60 * 1000)
        self.auto_sync_timer.timeout.connect(self._auto_sync_tick)
        # "3 分钟前" that never ticks becomes a lie, and the longer it stays on
        # screen the bigger the lie gets.  One label, once a minute.
        self.scan_age_timer = QTimer(self)
        self.scan_age_timer.setInterval(60 * 1000)
        self.scan_age_timer.timeout.connect(self._refresh_scan_age)
        self.notifier = AlertNotifier(config_store, state_store)
        # Alerting runs on its own thread and is started unconditionally: with
        # notifications off each pass costs one config read and stops there.
        self.alert_worker = AlertWorker(self.notifier)
        self.setWindowIcon(icon)
        self.setWindowTitle("SMSI 归档同步")
        self.setMinimumSize(1040, 680)
        self.resize(1040, 680)
        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self._load_form(self.config)
        self._populate_tree(self.catalog)
        self._center_window()
        self.busy_operation = "startup"
        self._update_busy_state()
        self._update_floating_idle_state()
        if self.config.auto_sync_enabled:
            self.auto_sync_timer.start()
        self.scan_age_timer.start()
        self.alert_worker.start()
        QTimer.singleShot(0, self._load_cached_catalog_async)

    def _center_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def _icon(self, name: str, token: str = "icon") -> QIcon:
        """A themed icon.

        The colour is named as a palette token instead of being written in, so
        the same call site serves every theme.  Icons are cached per name and
        colour, so a switch re-renders each glyph once and is then free.
        """
        return lucide_icon(name, getattr(self.theme.palette, token))

    def _apply_theme_to_application(self) -> None:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_theme(application, self.theme)

    def set_theme(self, name: str) -> None:
        """Switch theme on a live window.

        Nothing is rebuilt.  The stylesheet carries the colours and the control
        heights; this pushes the numbers the stylesheet cannot reach - layout
        margins, spacings, fixed widths, how the overview strip is arranged -
        into the layouts that were kept for exactly this, then re-measures the
        two places that fit themselves to the font.  Rebuilding instead would
        throw away the tree's expanded dates and the unsaved settings form.
        """
        theme = resolve_theme(name)
        if theme.name == self.theme.name:
            return
        self.theme = theme
        self.config.theme = theme.name
        self._apply_theme_to_application()
        self._apply_theme_layout()
        self._save_runtime_preferences()

    def _apply_theme_layout(self) -> None:
        metrics = self.theme.metrics
        self.top_bar.setFixedHeight(metrics.top_bar_height)
        self.top_bar_layout.setContentsMargins(*metrics.top_bar_margins)
        self.top_bar_layout.setSpacing(metrics.top_bar_spacing)
        self.top_bar_spacer.changeSize(metrics.top_bar_gap, 0)
        for layout in self._page_layouts:
            layout.setContentsMargins(*metrics.page_margins)
            layout.setSpacing(metrics.page_spacing)
        for layout in self._card_layouts:
            layout.setContentsMargins(*metrics.card_margins)
            layout.setSpacing(metrics.card_spacing)
        self.settings_content.setMaximumWidth(metrics.content_max_width)
        self.settings_content_layout.setSpacing(metrics.page_spacing)
        for form in self._forms:
            self._apply_form_metrics(form)
        for widget, kind in self._sized_inputs:
            widget.setFixedWidth(getattr(metrics, f"{kind}_width"))
        self.task_layout.setContentsMargins(*metrics.task_margins)
        self.task_layout.setSpacing(metrics.task_spacing)
        self.cancel_button.setFixedWidth(metrics.cancel_width)
        self._arrange_summary()
        self._apply_tree_metrics()
        self._align_form_labels()
        self._sync_theme_controls()
        self._refresh_static_icons()
        self.floating_window.set_theme(self.theme)
        # Item colours and per-item icons are painted, not styled, so the tree is
        # the one surface that has to be drawn again from the catalogue.
        self._populate_tree(self.catalog)
        self._render_disk_metric()
        self._update_auto_sync_chip()

    def _refresh_static_icons(self) -> None:
        """Re-colour every icon the window keeps a handle on."""
        self._set_page(self.pages.currentIndex())
        self.refresh_button.setIcon(self._icon("refresh-cw"))
        self.sync_button.setIcon(self._icon("download", "on_accent"))
        self.verify_button.setIcon(self._icon("shield-check", "accent"))
        self.cancel_button.setIcon(self._icon("x", "danger"))
        self.test_button.setIcon(self._icon("plug-zap", "accent"))
        self.save_button.setIcon(self._icon("save", "on_accent"))
        self.alert_test_button.setIcon(self._icon("send", "accent"))
        self.browse_button.setIcon(self._icon("folder-open"))
        for action, name, token in (
            (self.show_window_action, "panel-top-open", "accent"),
            (self.auto_sync_action, "refresh-cw", "accent"),
            (self.stop_sync_action, "square", "danger"),
            (self.floating_window_action, "picture-in-picture-2", "icon"),
            (self.exit_action, "power", "danger"),
        ):
            if action is not None:
                action.setIcon(self._icon(name, token))
        for name, action in self._theme_actions.items():
            action.setIcon(self._icon(
                "check" if name == self.theme.name else "circle", "accent"
            ))

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top_bar())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_sync_page())
        self.pages.addWidget(self._build_settings_page())
        outer.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)
        # The bar used to repeat the task title that the band two rows above it
        # was already showing.  Whether unattended syncing is armed is shown
        # nowhere else in the window - only in the tray menu - so that is what
        # earns the permanent slot.
        self.auto_sync_chip = QLabel()
        self.auto_sync_chip.setObjectName("statusChip")
        self.statusBar().addPermanentWidget(self.auto_sync_chip)
        self._update_auto_sync_chip()

    def _update_auto_sync_chip(self) -> None:
        enabled = bool(self.config.auto_sync_enabled)
        self.auto_sync_chip.setText(
            "自动同步 已开启" if enabled else "自动同步 已关闭"
        )
        self.auto_sync_chip.setProperty("state", "on" if enabled else "off")
        self.auto_sync_chip.setToolTip(
            "每 15 分钟检查一次 Ubuntu 并同步可用归档；可在托盘菜单切换"
            if enabled else
            "只有手动点击才会扫描或同步；可在托盘菜单开启"
        )
        self.auto_sync_chip.style().unpolish(self.auto_sync_chip)
        self.auto_sync_chip.style().polish(self.auto_sync_chip)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray = QSystemTrayIcon(self.icon, self)
        tray.setToolTip("SMSI 归档同步 · Ubuntu → Windows")
        menu = QMenu(self)
        self.tray_menu = menu

        self.show_window_action = QAction(
            self._icon("panel-top-open", "accent"), "显示主窗口", self
        )
        self.show_window_action.triggered.connect(self._show_from_tray)
        menu.addAction(self.show_window_action)
        menu.addSeparator()

        self.auto_sync_action = QAction(
            self._icon("refresh-cw", "accent"), "自动同步", self
        )
        self.auto_sync_action.setCheckable(True)
        self.auto_sync_action.setChecked(self.config.auto_sync_enabled)
        self.auto_sync_action.toggled.connect(self._set_auto_sync)
        menu.addAction(self.auto_sync_action)

        self.stop_sync_action = QAction(
            self._icon("square", "danger"), "停止同步", self
        )
        self.stop_sync_action.triggered.connect(self.stop_all_sync)
        menu.addAction(self.stop_sync_action)
        menu.addSeparator()

        self.floating_window_action = QAction(
            self._icon("picture-in-picture-2"), "显示悬浮窗", self
        )
        self.floating_window_action.setCheckable(True)
        self.floating_window_action.setChecked(self.config.floating_window_enabled)
        self.floating_window_action.toggled.connect(self._set_floating_window)
        menu.addAction(self.floating_window_action)
        # The window is normally hidden, so the tray menu is the only surface
        # some of this machine's operators ever see.  A theme reachable only from
        # a settings page they never open is a theme they do not have.
        menu.addMenu(self._build_theme_menu(menu))
        menu.addSeparator()

        self.exit_action = QAction(self._icon("power", "danger"), "退出客户端", self)
        self.exit_action.triggered.connect(self._exit_from_tray)
        menu.addAction(self.exit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray_icon = tray
        self._update_tray_state()

    def _build_theme_menu(self, parent: QMenu) -> QMenu:
        menu = QMenu("界面主题", parent)
        menu.setIcon(self._icon("palette"))
        group = QActionGroup(self)
        group.setExclusive(True)
        for theme in THEMES.values():
            action = QAction(
                self._icon(
                    "check" if theme.name == self.theme.name else "circle", "accent"
                ),
                theme.label,
                self,
            )
            action.setCheckable(True)
            action.setChecked(theme.name == self.theme.name)
            action.setToolTip(theme.caption)
            # triggered, not toggled: setChecked() from elsewhere must not look
            # like the user asking for a switch.
            action.triggered.connect(lambda _checked=False, name=theme.name: self.set_theme(name))
            group.addAction(action)
            menu.addAction(action)
            self._theme_actions[theme.name] = action
        self.theme_menu = menu
        return menu

    def _sync_theme_controls(self) -> None:
        """Point every switch at the theme that is actually on."""
        index = self.theme_input.findData(self.theme.name)
        if index >= 0 and index != self.theme_input.currentIndex():
            self.theme_input.setCurrentIndex(index)
        self.theme_caption.setText(self.theme.caption)
        for name, action in self._theme_actions.items():
            action.setChecked(name == self.theme.name)

    def _show_tray_menu_at(self, position: QPoint) -> None:
        if self.tray_menu is None:
            return
        self._update_tray_state()
        self.tray_menu.popup(position)

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._update_tray_state()

    def _hide_to_tray(self) -> None:
        if self.tray_icon is None:
            return
        self.hide()
        if not self._tray_notice_shown:
            self.tray_icon.showMessage(
                "SMSI 归档同步",
                "客户端已在后台运行，可从系统托盘重新打开。",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            self._tray_notice_shown = True
        self._update_tray_state()

    def _set_auto_sync(self, enabled: bool) -> None:
        self.config.auto_sync_enabled = enabled
        self._save_runtime_preferences()
        if enabled:
            self.auto_sync_timer.start()
            self.statusBar().showMessage("自动同步已开启", 5000)
            QTimer.singleShot(0, self._auto_sync_tick)
        else:
            self.auto_sync_timer.stop()
            self.statusBar().showMessage("自动同步已关闭", 5000)
        self._update_tray_state()
        self._update_floating_idle_state()

    def _set_floating_window(self, enabled: bool) -> None:
        self.config.floating_window_enabled = enabled
        if enabled:
            self._update_floating_idle_state()
            self.floating_window.show_near_work_area()
        else:
            self.floating_window.hide()
        self._update_tray_state()

    def _save_runtime_preferences(self) -> None:
        try:
            self.config_store.save(self.config)
        except (ValueError, RuntimeError):
            LOGGER.exception("Unable to save tray preferences")

    def _auto_sync_tick(self) -> None:
        if not self.config.auto_sync_enabled or self.busy_operation:
            return
        if self.config.validate(require_connection=True):
            return
        self.start_scan(automatic=True)

    def _update_tray_state(self) -> None:
        configured = not self.config.validate(require_connection=True)
        self._update_auto_sync_chip()
        if self.show_window_action is not None:
            self.show_window_action.setEnabled(not self.isVisible())
        if self.auto_sync_action is not None:
            self.auto_sync_action.blockSignals(True)
            self.auto_sync_action.setChecked(self.config.auto_sync_enabled)
            self.auto_sync_action.setEnabled(configured and not self._startup_loading)
            self.auto_sync_action.blockSignals(False)
        can_stop = self.config.auto_sync_enabled or self.busy_operation in {"sync", "auto_sync", "verify"}
        if self.stop_sync_action is not None:
            self.stop_sync_action.setEnabled(can_stop)
        if self.floating_window_action is not None:
            self.floating_window_action.blockSignals(True)
            self.floating_window_action.setChecked(self.config.floating_window_enabled)
            self.floating_window_action.setText(
                "隐藏悬浮窗" if self.config.floating_window_enabled else "显示悬浮窗"
            )
            self.floating_window_action.blockSignals(False)

    def stop_all_sync(self) -> None:
        was_running = self.busy_operation in {"sync", "auto_sync", "verify"}
        if self.config.auto_sync_enabled:
            self.config.auto_sync_enabled = False
            self.auto_sync_timer.stop()
            self._save_runtime_preferences()
        if was_running:
            self.cancel_event.set()
            self.task_title.setText("正在停止")
            self.task_detail.setText("等待当前文件安全停止")
            self.cancel_button.setEnabled(False)
        self._update_tray_state()
        self._update_floating_idle_state("正在停止" if was_running else "同步已停止")

    def _update_floating_idle_state(self, title: str = "空闲") -> None:
        mode = "自动" if self.config.auto_sync_enabled else "手动"
        detail = "自动同步已开启" if self.config.auto_sync_enabled else "自动同步未开启"
        self.floating_window.set_status(
            title, detail, 0, mode,
        )

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def _load_cached_catalog_async(self) -> None:
        def load() -> None:
            try:
                catalog = self.state_store.load_catalog()
            except Exception:
                LOGGER.exception("Unable to load cached catalog")
                catalog = None
            self.signals.cached_catalog_ready.emit(catalog)

        threading.Thread(target=load, name="smsi-load-cache", daemon=True).start()

    def _on_cached_catalog_ready(self, catalog: object) -> None:
        if isinstance(catalog, ArchiveCatalog):
            self.catalog = catalog
            self._populate_tree(catalog)
        self._startup_loading = False
        self.busy_operation = ""
        self._update_busy_state()
        if not self.config.validate(require_connection=True):
            self.start_scan(automatic=self.config.auto_sync_enabled)
        else:
            self.connection_label.setText("需要完成连接设置")
            self.task_title.setText("准备就绪")
            self.task_detail.setText("请先在设置中完成 Ubuntu 连接")

    def _exit_from_tray(self) -> None:
        if self.busy_operation in {"sync", "auto_sync", "verify"}:
            answer = QMessageBox.question(
                self,
                "退出客户端",
                "同步或校验正在进行。退出会取消当前任务，未完成文件会保留以便继续。\n\n确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.cancel_event.set()
        self._exit_requested = True
        self.floating_window.close()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _build_top_bar(self) -> QWidget:
        metrics = self.theme.metrics
        top_bar = QFrame()
        top_bar.setObjectName("appHeader")
        top_bar.setFixedHeight(metrics.top_bar_height)
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(*metrics.top_bar_margins)
        layout.setSpacing(metrics.top_bar_spacing)
        title = QLabel("SMSI 归档同步")
        title.setObjectName("brandTitle")
        layout.addWidget(title)
        layout.addSpacing(metrics.top_bar_gap)
        self.top_bar = top_bar
        self.top_bar_layout = layout
        # addSpacing leaves a spacer item behind that a later theme has to be
        # able to resize; itemAt is how it is found again.
        self.top_bar_spacer = layout.itemAt(layout.count() - 1)
        self.sync_nav = QPushButton("归档同步")
        self.sync_nav.setObjectName("topTab")
        self.sync_nav.setCheckable(True)
        self.sync_nav.setChecked(True)
        self.sync_nav.setIcon(self._icon("folder-sync", "accent"))
        self.settings_nav = QPushButton("设置")
        self.settings_nav.setObjectName("topTab")
        self.settings_nav.setCheckable(True)
        self.settings_nav.setIcon(self._icon("settings"))
        self.sync_nav.clicked.connect(lambda: self._set_page(0))
        self.settings_nav.clicked.connect(lambda: self._set_page(1))
        layout.addWidget(self.sync_nav)
        layout.addWidget(self.settings_nav)
        layout.addStretch(1)
        version = QLabel(f"版本 {__version__}")
        version.setObjectName("versionLabel")
        layout.addWidget(version)
        return top_bar

    def _set_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.sync_nav.setChecked(index == 0)
        self.settings_nav.setChecked(index == 1)
        self.sync_nav.setIcon(self._icon(
            "folder-sync", "accent" if index == 0 else "icon"
        ))
        self.settings_nav.setIcon(self._icon(
            "settings", "accent" if index == 1 else "icon"
        ))

    def _page_header(self, title: str, caption: str) -> tuple[QWidget, QHBoxLayout]:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        description = QLabel(caption)
        description.setObjectName("muted")
        labels.addWidget(heading)
        labels.addWidget(description)
        layout.addLayout(labels)
        layout.addStretch(1)
        return widget, layout

    def _build_sync_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*self.theme.metrics.page_margins)
        layout.setSpacing(self.theme.metrics.page_spacing)
        self._page_layouts.append(layout)
        header, actions = self._page_header("归档文件", f"Ubuntu  {REMOTE_ROOT}")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("toolbarButton")
        self.refresh_button.setIcon(self._icon("refresh-cw"))
        self.sync_button = QPushButton("同步可用归档")
        self.sync_button.setObjectName("primaryButton")
        self.sync_button.setIcon(self._icon("download", "on_accent"))
        self.verify_button = QPushButton("校验选中")
        self.verify_button.setObjectName("toolbarButton")
        self.verify_button.setIcon(self._icon("shield-check", "accent"))
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.verify_button)
        actions.addWidget(self.sync_button)
        layout.addWidget(header)
        layout.addWidget(self._build_summary_bar())

        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(("名称", "Ubuntu 状态", "归档大小", "本地大小", "进度", "Windows 验证", "内容"))
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # The last column is a fixed-shape sentence, the name column is the one
        # that grows without limit, so let the name take the spare width.
        header_view.setStretchLastSection(False)
        self._apply_tree_metrics()
        layout.addWidget(self.tree, 1)
        layout.addWidget(self._build_task_bar())
        return page

    def _apply_tree_metrics(self) -> None:
        """Column widths, icon size and the smallest a column may be dragged to.

        A pinned width is a bet that the heading fits inside it, and two of the
        original numbers lost it: "Ubuntu 状态" rendered as "Ubuntu 状".  Each
        column takes the wider of its theme's number and its own measured
        heading, so neither a larger theme font nor a display scale nobody chose
        these numbers for can cut a column title in half.

        Polish first.  A fresh header still carries Qt's 9pt default, and the
        stylesheet's size only arrives with the polish, so measuring too early
        asks the wrong font and comes back short - which is how much of the
        heading then went missing.
        """
        metrics = self.theme.metrics
        self.tree.setIconSize(QSize(metrics.tree_icon, metrics.tree_icon))
        header_view = self.tree.header()
        header_view.ensurePolished()
        font_metrics = QFontMetrics(header_view.font())
        for column, width in metrics.tree_columns:
            heading = self.tree.headerItem().text(column)
            # 8px of section padding on each side, 1px separator, and the
            # headings are drawn semi-bold while the metrics are not.
            needed = int(font_metrics.horizontalAdvance(heading) * 1.08) + 18
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(column, max(width, needed))
        header_view.setMinimumSectionSize(metrics.tree_min_section)

    def _build_summary_bar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("summaryBar")
        # A grid rather than a row of boxes: this is the one part of the window a
        # theme is allowed to reshape, and reflowing eight readings from one long
        # strip into a 4x2 board must not mean deleting and rebuilding the labels
        # that other code already holds references to.
        grid = QGridLayout(frame)
        self._summary_grid = grid
        self.connection_label = self._metric("连接", "未检查")
        self.ready_label = self._metric("可同步日期", "0")
        self.pending_label = self._metric("待同步数据", "0 B")
        self.disk_label = self._metric("归档盘可用", "--")
        self.verified_label = self._metric("本地已验证", "0")
        self.continuity_label = self._metric("连续完整", "0 天")
        self.gap_label = self._metric("日期缺口", "0")
        self.last_scan_label = self._metric("最近扫描", "--")
        for _ in range(len(self._summary_cells) - 1):
            divider = QFrame(frame)
            divider.setObjectName("metricDivider")
            self._summary_dividers.append(divider)
        self._arrange_summary()
        return frame

    def _metric(self, label: str, value: str) -> QLabel:
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        value_label = QLabel(value)
        value_label.setObjectName("metricValue")
        label_widget = QLabel(label)
        label_widget.setObjectName("metricLabel")
        box.addWidget(value_label)
        box.addWidget(label_widget)
        self._summary_cells.append(widget)
        self._summary_boxes.append(box)
        return value_label

    def _arrange_summary(self) -> None:
        """Place the readings for the current theme.

        Eight readings in one row with a hairline between each is a dense
        administrative strip; the same eight in four columns and two rows, each
        reading twice the point size and no rules between them, is a board.  Same
        widgets, same numbers, and the difference is legible from the doorway.
        """
        metrics = self.theme.metrics
        grid = self._summary_grid
        grid.setContentsMargins(*metrics.summary_margins)
        grid.setHorizontalSpacing(metrics.summary_spacing)
        grid.setVerticalSpacing(metrics.summary_row_spacing)
        for cell in self._summary_cells:
            grid.removeWidget(cell)
        for divider in self._summary_dividers:
            grid.removeWidget(divider)
        for column in range(grid.columnCount()):
            grid.setColumnStretch(column, 0)
        columns = max(1, metrics.summary_columns)
        # A hairline only separates neighbours in a single row; between the rows
        # of a board it would be a grid nobody asked for.
        ruled = metrics.summary_dividers and columns >= len(self._summary_cells)
        for index, cell in enumerate(self._summary_cells):
            row, position = divmod(index, columns)
            grid.addWidget(cell, row, position * 2 if ruled else position)
            cell.setMinimumWidth(metrics.metric_min_widths[index])
            self._summary_boxes[index].setSpacing(metrics.metric_gap)
        for index, divider in enumerate(self._summary_dividers):
            if ruled:
                divider.setFixedSize(1, metrics.summary_divider_height)
                grid.addWidget(divider, 0, index * 2 + 1)
            divider.setVisible(ruled)
        if ruled:
            # One trailing column takes the slack, so the readings stay left and
            # keep the widths they were given.
            grid.setColumnStretch(len(self._summary_cells) * 2 - 1, 1)
        else:
            # No slack column: the board spreads its columns evenly instead.
            for column in range(columns):
                grid.setColumnStretch(column, 1)

    def _build_task_bar(self) -> QWidget:
        metrics = self.theme.metrics
        frame = QFrame()
        frame.setObjectName("taskBar")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*metrics.task_margins)
        layout.setSpacing(metrics.task_spacing)
        self.task_layout = layout
        top = QHBoxLayout()
        self.task_title = QLabel("空闲")
        self.task_title.setObjectName("sectionTitle")
        self.task_detail = QLabel("没有运行中的任务")
        self.task_detail.setObjectName("muted")
        self.task_detail.setMinimumWidth(0)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setIcon(self._icon("x", "danger"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.setFixedWidth(metrics.cancel_width)
        top.addWidget(self.task_title)
        top.addSpacing(10)
        top.addWidget(self.task_detail, 1)
        top.addWidget(self.cancel_button)
        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 1000)
        self.task_progress.setValue(0)
        self.task_progress.setTextVisible(False)
        layout.addLayout(top)
        layout.addWidget(self.task_progress)
        return frame

    def _build_settings_page(self) -> QWidget:
        metrics = self.theme.metrics
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(*metrics.page_margins)
        outer.setSpacing(metrics.page_spacing)
        self._page_layouts.append(outer)
        header, actions = self._page_header("客户端设置", "Ubuntu 连接、Windows 存储与传输限制")
        self.test_button = QPushButton("测试连接")
        self.test_button.setIcon(self._icon("plug-zap", "accent"))
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setIcon(self._icon("save", "on_accent"))
        actions.addWidget(self.test_button)
        actions.addWidget(self.save_button)
        outer.addWidget(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setMaximumWidth(metrics.content_max_width)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(metrics.page_spacing)
        self.settings_content = content
        self.settings_content_layout = content_layout
        content_layout.addWidget(self._connection_section())
        content_layout.addWidget(self._storage_section())
        content_layout.addWidget(self._transfer_section())
        content_layout.addWidget(self._alert_section())
        content_layout.addWidget(self._appearance_section())
        content_layout.addStretch(1)
        self._align_form_labels()
        scroll.setWidget(content)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        outer.addWidget(scroll, 1)
        return page

    def _settings_form(self) -> QFormLayout:
        form = QFormLayout()
        self._forms.append(form)
        self._apply_form_metrics(form)
        return form

    def _apply_form_metrics(self, form: QFormLayout) -> None:
        """The spacings and the row shape, both of which a theme owns.

        Wrapping every row puts the label on its own line above its field.  That
        is what lets a wider control and a larger font still fit a page narrower
        than this one, and it is why the label column is not measured at all in a
        theme that wraps.
        """
        metrics = self.theme.metrics
        form.setHorizontalSpacing(metrics.form_h_spacing)
        form.setVerticalSpacing(metrics.form_v_spacing)
        form.setLabelAlignment(self._form_label_alignment())
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
            if metrics.form_wrap_rows
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )

    def _form_label_alignment(self) -> Qt.AlignmentFlag:
        horizontal = (
            Qt.AlignmentFlag.AlignRight
            if self.theme.metrics.label_align_right
            else Qt.AlignmentFlag.AlignLeft
        )
        return horizontal | Qt.AlignmentFlag.AlignVCenter

    def _form_row(self, form: QFormLayout, text: str, field: QWidget) -> None:
        label = QLabel(text)
        label.setAlignment(self._form_label_alignment())
        self._form_labels.append(label)
        form.addRow(label, field)

    def _sized_input(self, widget: QWidget, kind: str) -> QWidget:
        """Register a control whose width is a theme number, and apply it now."""
        self._sized_inputs.append((widget, kind))
        widget.setFixedWidth(getattr(self.theme.metrics, f"{kind}_width"))
        return widget

    def _align_form_labels(self) -> None:
        """One label column for the whole page, not one per card.

        A form layout sizes its label column from its own longest label, so the
        card whose longest label was three characters put its inputs 26px left
        of the card below it.  Measured after polishing because the point size
        arrives with the stylesheet, not with the widget.

        A theme that stacks its labels above its fields has no column to share,
        so the minimum is released rather than left behind as blank padding.
        """
        alignment = self._form_label_alignment()
        for label in self._form_labels:
            label.setAlignment(alignment)
            label.ensurePolished()
        if self.theme.metrics.form_wrap_rows:
            for label in self._form_labels:
                label.setMinimumWidth(0)
            return
        widest = max(
            [label.sizeHint().width() for label in self._form_labels]
            + [QFontMetrics(self.font()).horizontalAdvance(FORM_LABEL_SAMPLE)]
        )
        for label in self._form_labels:
            label.setMinimumWidth(widest)

    def _section(self, title: str, description: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("settingsSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(*self.theme.metrics.card_margins)
        layout.setSpacing(self.theme.metrics.card_spacing)
        self._card_layouts.append(layout)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        caption = QLabel(description)
        caption.setObjectName("sectionDescription")
        layout.addWidget(heading)
        layout.addWidget(caption)
        return frame, layout

    def _connection_section(self) -> QWidget:
        frame, layout = self._section("Ubuntu 连接", "只读 SSH 连接，主机指纹确认后才允许同步")
        form = self._settings_form()
        self.host_input = QLineEdit()
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self._sized_input(self.port_input, "port")
        self.username_input = QLineEdit()
        self.remote_root_input = QLineEdit(REMOTE_ROOT)
        self.remote_root_input.setReadOnly(True)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Ubuntu SSH 密码")
        self.fingerprint_input = QLineEdit()
        self.fingerprint_input.setReadOnly(True)
        server_row = QWidget()
        server_layout = QHBoxLayout(server_row)
        server_layout.setContentsMargins(0, 0, 0, 0)
        server_layout.setSpacing(9)
        port_label = QLabel("端口")
        port_label.setObjectName("inlineLabel")
        server_layout.addWidget(self.host_input, 1)
        server_layout.addWidget(port_label)
        server_layout.addWidget(self.port_input)
        self._form_row(form, "服务器地址", server_row)
        self._form_row(form, "用户名", self.username_input)
        self._form_row(form, "SSH 密码", self.password_input)
        self._form_row(form, "主机指纹", self.fingerprint_input)
        self._form_row(form, "远端目录", self.remote_root_input)
        layout.addLayout(form)
        return frame

    def _storage_section(self) -> QWidget:
        frame, layout = self._section(
            "Windows 存储",
            "正式副本与未完成临时文件位于同一个归档盘；可用空间低于保留值时暂停下载",
        )
        form = self._settings_form()
        self.local_root_input = QLineEdit()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        button = QPushButton("选择")
        self.browse_button = button
        self._sized_input(button, "browse")
        button.setIcon(self._icon("folder-open"))
        button.clicked.connect(self._choose_local_root)
        row_layout.addWidget(self.local_root_input, 1)
        row_layout.addWidget(button)
        self.reserve_input = QSpinBox()
        self.reserve_input.setRange(1, 1024)
        self.reserve_input.setSuffix(" GiB")
        self._sized_input(self.reserve_input, "number")
        self._form_row(form, "本地归档目录", row)
        self._form_row(form, "磁盘保留空间", self.reserve_input)
        layout.addLayout(form)
        return frame

    def _transfer_section(self) -> QWidget:
        frame, layout = self._section("同步限制", "带宽为整个客户端的总上限")
        form = self._settings_form()
        self.workers_input = QSpinBox()
        self.workers_input.setRange(1, 4)
        self._sized_input(self.workers_input, "number")
        self.bandwidth_input = QComboBox()
        self._sized_input(self.bandwidth_input, "choice")
        for label, value in (("不限速", 0), ("5 MiB/s", 5), ("10 MiB/s", 10), ("20 MiB/s", 20), ("50 MiB/s", 50)):
            self.bandwidth_input.addItem(label, value)
        self._form_row(form, "并发任务", self.workers_input)
        self._form_row(form, "总带宽", self.bandwidth_input)
        layout.addLayout(form)
        return frame

    def _alert_section(self) -> QWidget:
        frame, layout = self._section(
            "告警通知", "窗口最小化时，失败与停更通过 Telegram 主动通知"
        )
        form = self._settings_form()
        self.alert_enabled_input = QComboBox()
        self._sized_input(self.alert_enabled_input, "choice")
        self.alert_enabled_input.addItem("关闭", False)
        self.alert_enabled_input.addItem("开启", True)
        self.alert_token_input = QLineEdit()
        self.alert_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.alert_token_input.setPlaceholderText("留空表示不修改已保存的令牌")
        self.alert_chat_input = QLineEdit()
        self.alert_chat_input.setPlaceholderText("例如 -1001234567890")
        self.alert_level_input = QComboBox()
        self._sized_input(self.alert_level_input, "choice")
        self.alert_level_input.addItem("警告及以上", "warning")
        self.alert_level_input.addItem("仅错误", "error")
        self.alert_stale_input = QSpinBox()
        self.alert_stale_input.setRange(1, 168)
        self.alert_stale_input.setSuffix(" 小时")
        self._sized_input(self.alert_stale_input, "number")
        self.alert_lag_input = QSpinBox()
        self.alert_lag_input.setRange(2, 14)
        self.alert_lag_input.setSuffix(" 天")
        self._sized_input(self.alert_lag_input, "number")
        self.alert_test_button = QPushButton("发送测试消息")
        self.alert_test_button.setIcon(self._icon("send", "accent"))
        test_row = QWidget()
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.addWidget(self.alert_test_button)
        test_layout.addStretch(1)
        self._form_row(form, "告警通知", self.alert_enabled_input)
        self._form_row(form, "机器人令牌", self.alert_token_input)
        self._form_row(form, "会话 ID", self.alert_chat_input)
        self._form_row(form, "推送级别", self.alert_level_input)
        self._form_row(form, "扫描中断", self.alert_stale_input)
        self._form_row(form, "数据源停更", self.alert_lag_input)
        self._form_row(form, "", test_row)
        layout.addLayout(form)
        self.alert_health_label = QLabel()
        self.alert_health_label.setObjectName("sectionDescription")
        self.alert_health_label.setWordWrap(True)
        layout.addWidget(self.alert_health_label)
        return frame

    def _appearance_section(self) -> QWidget:
        frame, layout = self._section(
            "界面主题", "主题同时决定配色和排版；切换立即生效，不需要重启"
        )
        form = self._settings_form()
        self.theme_input = QComboBox()
        self._sized_input(self.theme_input, "choice")
        for theme in THEMES.values():
            self.theme_input.addItem(theme.label, theme.name)
        self.theme_input.setCurrentIndex(
            max(0, self.theme_input.findData(self.theme.name))
        )
        # Applied on selection rather than on 保存: a theme is judged by looking
        # at it, and a preview you have to commit to is not a preview.  It is
        # written to the config file the same moment, the way the tray toggles
        # are, so the choice survives a restart without touching the form below.
        self.theme_input.currentIndexChanged.connect(self._on_theme_selected)
        self._form_row(form, "主题", self.theme_input)
        layout.addLayout(form)
        self.theme_caption = QLabel(self.theme.caption)
        self.theme_caption.setObjectName("sectionDescription")
        self.theme_caption.setWordWrap(True)
        layout.addWidget(self.theme_caption)
        return frame

    def _on_theme_selected(self, index: int) -> None:
        name = self.theme_input.itemData(index)
        if isinstance(name, str):
            self.set_theme(name)

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.start_scan)
        self.sync_button.clicked.connect(self.start_sync)
        self.verify_button.clicked.connect(self.start_verify)
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.save_button.clicked.connect(self.save_settings)
        self.test_button.clicked.connect(self.test_connection)
        self.alert_test_button.clicked.connect(self.send_alert_test)
        self.signals.alert_test_done.connect(self._on_alert_test_done)
        self.tree.itemSelectionChanged.connect(self._update_action_state)
        self.tree.itemExpanded.connect(self._on_tree_item_expanded)
        self.tree.itemDoubleClicked.connect(self._show_item_detail)
        self.signals.cached_catalog_ready.connect(self._on_cached_catalog_ready)
        self.signals.catalog_ready.connect(self._on_catalog_ready)
        self.signals.operation_progress.connect(self._on_progress)
        self.signals.operation_done.connect(self._on_operation_done)
        self.signals.operation_error.connect(self._on_operation_error)
        self.signals.host_key_required.connect(self._on_host_key_required)

    def _load_form(self, config: AppConfig) -> None:
        self.host_input.setText(config.host)
        self.port_input.setValue(config.port)
        self.username_input.setText(config.username)
        try:
            self.password_input.setText(config.password())
        except RuntimeError:
            self.password_input.clear()
        self.fingerprint_input.setText(config.host_fingerprint)
        self.local_root_input.setText(config.local_root)
        self.reserve_input.setValue(int(config.minimum_free_gib))
        self.workers_input.setValue(config.download_workers)
        index = self.bandwidth_input.findData(config.bandwidth_mib)
        self.bandwidth_input.setCurrentIndex(max(0, index))
        self.alert_enabled_input.setCurrentIndex(
            max(0, self.alert_enabled_input.findData(bool(config.alert_enabled)))
        )
        # The stored token is never loaded back into the field: an empty box
        # means "keep it", so a save of unrelated settings cannot wipe it.
        self.alert_token_input.clear()
        self.alert_chat_input.setText(config.alert_chat_id)
        level = config.alert_min_level if config.alert_min_level in ALERT_LEVELS else "warning"
        self.alert_level_input.setCurrentIndex(max(0, self.alert_level_input.findData(level)))
        self.alert_stale_input.setValue(int(config.alert_stale_hours))
        self.alert_lag_input.setValue(int(config.alert_upstream_lag_days))
        self._render_alert_health(config)

    def _render_alert_health(self, config: AppConfig) -> None:
        if not config.alert_enabled:
            self.alert_health_label.setText("告警通知未启用，失败只能在本窗口看到。")
            return
        health = self.notifier.state()
        parts: list[str] = []
        if health["last_error"]:
            parts.append(f"最近一次发送失败：{health['last_error']}")
        elif health["last_sent_at"]:
            parts.append(f"最近推送 {timestamp_text(health['last_sent_at'])}")
        else:
            parts.append("已启用，暂无需要推送的内容")
        if health["scan_alerted"]:
            parts.append("当前处于「扫描中断」告警状态")
        if health["upstream_alerted"]:
            parts.append("当前处于「数据源停更」告警状态")
        if health["disk_alerted"]:
            parts.append("当前处于「归档盘异常」告警状态")
        if health["pending_events"]:
            parts.append("仍有待推送内容")
        self.alert_health_label.setText("；".join(parts) + "。")

    def _form_config(self) -> AppConfig:
        config = AppConfig(
            host=self.host_input.text().strip(),
            port=self.port_input.value(),
            username=self.username_input.text().strip(),
            password_blob="",
            host_fingerprint=self.fingerprint_input.text().strip(),
            remote_root=REMOTE_ROOT,
            local_root=self.local_root_input.text().strip(),
            minimum_free_gib=self.reserve_input.value(),
            download_workers=self.workers_input.value(),
            bandwidth_mib=int(self.bandwidth_input.currentData()),
            auto_sync_enabled=self.config.auto_sync_enabled,
            floating_window_enabled=self.config.floating_window_enabled,
            alert_enabled=bool(self.alert_enabled_input.currentData()),
            alert_token_blob=self.config.alert_token_blob,
            alert_chat_id=self.alert_chat_input.text().strip(),
            alert_min_level=str(self.alert_level_input.currentData()),
            alert_stale_hours=self.alert_stale_input.value(),
            alert_upstream_lag_days=self.alert_lag_input.value(),
            # Applied and stored the moment it is picked, so the form must carry
            # the live value forward - saving the page must not quietly undo a
            # theme chosen from the tray a minute ago.
            theme=self.config.theme,
        )
        config.set_password(self.password_input.text())
        token = validate_alert_token(self.alert_token_input.text())
        if token:
            config.set_alert_token(token)
        return config

    def _choose_local_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 Windows 归档目录", self.local_root_input.text())
        if path:
            self.local_root_input.setText(path)

    def save_settings(self) -> None:
        try:
            config = self._form_config()
            self.config_store.save(config)
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "设置未保存", str(exc))
            return
        self.config = config
        self.alert_token_input.clear()
        self._render_alert_health(config)
        # A changed reserve or archive directory must show up now, not at the
        # next scan: this is the number the user came here to check.
        self._render_disk_metric()
        # An alert setting is useless if it waits for the next poll interval.
        self.alert_worker.wake()
        self.statusBar().showMessage("设置已保存", 5000)
        self._set_page(0)
        if not config.validate(require_connection=True):
            self.start_scan()

    def send_alert_test(self) -> None:
        """Prove the channel from the settings page, off the UI thread."""
        self.alert_test_button.setEnabled(False)

        def work() -> None:
            try:
                self.notifier.send_test()
            except (RuntimeError, OSError) as exc:
                self.signals.alert_test_done.emit(False, str(exc)[:300])
            else:
                self.signals.alert_test_done.emit(True, "")

        threading.Thread(target=work, name="smsi-alert-test", daemon=True).start()

    def _on_alert_test_done(self, ok: bool, message: str) -> None:
        self.alert_test_button.setEnabled(True)
        self._render_alert_health(self.config)
        if ok:
            self.statusBar().showMessage("测试消息已发送，请查看 Telegram", 6000)
            QMessageBox.information(self, "测试消息已发送", "请在 Telegram 中确认收到消息。")
        else:
            QMessageBox.warning(self, "测试消息发送失败", message)

    def _start_background(self, operation: str, target: Callable[[], Any]) -> bool:
        if self.busy_operation:
            return False
        self.busy_operation = operation
        self.cancel_event = threading.Event()
        self._update_busy_state()

        def run() -> None:
            try:
                result = target()
            except HostKeyRequired as exc:
                self.signals.host_key_required.emit(exc.fingerprint, exc.key_type)
            except Exception as exc:
                LOGGER.exception("Operation failed: %s", operation)
                self.signals.operation_error.emit(operation, str(exc))
            else:
                self.signals.operation_done.emit(operation, result)

        threading.Thread(target=run, name=f"smsi-{operation}", daemon=True).start()
        return True

    def test_connection(self) -> None:
        config = self._form_config()

        def work() -> dict[str, Any]:
            with RemoteArchiveClient(config) as client:
                collectors = client.list_directories(config.remote_root)
            return {"collectors": len([name for name in collectors if name.startswith("collector=")])}

        if self._start_background("test", work):
            self.task_title.setText("测试连接")
            self.task_detail.setText(f"{config.host}:{config.port}")
            self.task_progress.setRange(0, 0)

    def start_scan(self, *, automatic: bool = False) -> None:
        errors = self.config.validate(require_connection=True)
        if errors:
            self._set_page(1)
            QMessageBox.warning(self, "连接设置不完整", "\n".join(errors))
            return

        def on_scan(message: str, current: int, total: int) -> None:
            self.signals.operation_progress.emit({
                "phase": "scanning", "message": message, "current": current, "total": total
            })

        def work() -> ArchiveCatalog:
            with RemoteArchiveClient(self.config) as client:
                return scan_catalog(
                    self.config, client, on_scan, previous=self.catalog
                )

        operation = "auto_scan" if automatic else "scan"
        if self._start_background(operation, work):
            self.task_title.setText("扫描 Ubuntu")
            self.task_detail.setText(REMOTE_ROOT)
            self.task_progress.setRange(0, 0)

    def _selected_days(self, *, default_all: bool) -> list[ArchiveDay]:
        by_key = {day.key: day for day in self.catalog.days}
        selected = self.tree.selectedItems()
        if not selected:
            return list(self.catalog.days) if default_all else []
        keys: set[str] = set()
        for item in selected:
            data = item.data(0, NODE_ROLE) or {}
            kind = data.get("kind")
            if kind == "root":
                keys.update(by_key)
            elif kind == "collector":
                keys.update(key for key, day in by_key.items() if day.collector_id == data.get("collector_id"))
            elif data.get("day_key") in by_key:
                keys.add(data["day_key"])
        return [by_key[key] for key in sorted(keys)]

    def start_sync(self) -> None:
        explicitly_selected = bool(self.tree.selectedItems())
        days = [
            day for day in self._selected_days(default_all=True) if day.eligible
        ]
        if not days:
            QMessageBox.information(self, "没有可同步内容", "选中范围内没有已经通过 Ubuntu 验证的待同步归档。")
            return
        total = sum(day.bytes_total - day.bytes_local for day in days)
        scope_note = (
            "将同步选中的历史归档。"
            if explicitly_selected
            else "当前没有选中项目，将同步全部可用归档。"
        )
        answer = QMessageBox.question(
            self,
            "开始归档同步",
            f"{scope_note}\n共 {len(days)} 个归档日期，预计传输 {bytes_text(total)}。\n\n本操作不会删除 Ubuntu 或 Windows 文件。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_sync_days(days, "sync")

    def _start_sync_days(self, days: list[ArchiveDay], operation: str) -> bool:
        def progress(payload: dict[str, Any]) -> None:
            self.signals.operation_progress.emit(payload)

        def work() -> list[dict[str, Any]]:
            engine = SyncEngine(self.config, cancel=self.cancel_event, progress=progress)
            return engine.sync_days(days)

        if self._start_background(operation, work):
            self.task_title.setText("准备同步")
            self.task_detail.setText(f"{len(days)} 个归档日期")
            self.task_progress.setRange(0, 1000)
            self.task_progress.setValue(0)
            return True
        return False

    def start_verify(self) -> None:
        days = self._selected_days(default_all=False)
        selected = [
            day
            for day in days
            if day.local_status in {"verified", "local_unverified", "verification_failed"}
        ]
        if not selected:
            QMessageBox.information(self, "没有可校验内容", "请选择已有 Windows 本地副本的归档日期。")
            return
        answer = QMessageBox.question(
            self,
            "重新完整校验",
            f"将重新读取并校验 {len(selected)} 个归档日期。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def progress(payload: dict[str, Any]) -> None:
            self.signals.operation_progress.emit(payload)

        def work() -> list[dict[str, Any]]:
            engine = SyncEngine(self.config, cancel=self.cancel_event, progress=progress)
            return engine.verify_days(selected)

        if self._start_background("verify", work):
            self.task_title.setText("准备校验")
            self.task_detail.setText(f"{len(selected)} 个归档日期")
            self.task_progress.setRange(0, 1000)
            self.task_progress.setValue(0)

    def cancel_current_operation(self) -> None:
        if self.busy_operation in {"sync", "auto_sync", "verify", "scan", "auto_scan"}:
            self.cancel_event.set()
            self.task_title.setText("正在取消")
            self.task_detail.setText("等待当前文件安全停止")
            self.cancel_button.setEnabled(False)

    def _on_host_key_required(self, fingerprint: str, key_type: str) -> None:
        operation = self.busy_operation
        self.busy_operation = ""
        self._update_busy_state()
        answer = QMessageBox.question(
            self,
            "确认 Ubuntu 主机指纹",
            f"主机：{self.host_input.text().strip()}\n类型：{key_type}\n指纹：{fingerprint}\n\n请确认该指纹与 Ubuntu 服务器显示的一致。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.fingerprint_input.setText(fingerprint)
            try:
                config = self._form_config()
                self.config_store.save(config)
                self.config = config
            except (ValueError, RuntimeError) as exc:
                self.statusBar().showMessage(f"主机指纹已读取，但设置保存失败：{exc}", 10000)
                return
            self.statusBar().showMessage("主机指纹已确认并保存", 5000)
            if operation == "test":
                self.test_connection()
            elif operation == "scan":
                self.start_scan()

    def _on_progress(self, payload: dict[str, Any]) -> None:
        phase = str(payload.get("phase") or "")
        if phase == "scanning":
            current, total = int(payload.get("current") or 0), int(payload.get("total") or 0)
            self.task_title.setText("扫描 Ubuntu")
            self.task_detail.setText(str(payload.get("message") or REMOTE_ROOT))
            if total:
                self.task_progress.setRange(0, 1000)
                self.task_progress.setValue(int(current * 1000 / total))
            else:
                self.task_progress.setRange(0, 0)
            self.floating_window.set_status(
                "扫描",
                str(payload.get("message") or REMOTE_ROOT),
                int(current * 1000 / total) if total else 0,
                f"{current}/{total}" if total else "--",
                indeterminate=not total,
            )
            return
        archive_date = str(payload.get("archive_date") or "")
        collector_id = str(payload.get("collector_id") or "")
        current_object = str(payload.get("current_object") or "")
        total_bytes = int(payload.get("bytes_total") or 0)
        done_bytes = int(payload.get("bytes_done") or 0)
        total_objects = int(payload.get("object_count") or 0)
        done_objects = int(payload.get("objects_done") or 0)
        if phase == "syncing":
            current_fraction = int(done_bytes * 1000 / total_bytes) if total_bytes else 0
        else:
            current_fraction = int(done_objects * 1000 / total_objects) if total_objects else 0
        self.task_progress.setRange(0, 1000)
        self.task_progress.setValue(current_fraction)
        self._update_live_tree(payload)
        if phase == "syncing":
            speed = bytes_text(payload.get("speed_bytes_per_second") or 0) + "/s"
            eta = duration_text(payload.get("eta_seconds"))
            self.task_title.setText(f"正在同步 {archive_date}")
            self.task_detail.setText(
                f"{collector_id} · {done_objects}/{total_objects} · {bytes_text(done_bytes)}/{bytes_text(total_bytes)} · {speed} · 剩余 {eta}"
            )
            self.floating_window.set_status(
                f"{current_fraction / 10:.1f}%",
                f"正在同步 {collector_id} · {archive_date}",
                current_fraction,
                speed,
            )
        elif phase == "verifying":
            verification_level = str(payload.get("verification_level") or "deep")
            verification_label = "传输校验" if verification_level == "transport" else "完整校验"
            self.task_title.setText(f"正在{verification_label} {archive_date}")
            self.task_detail.setText(f"{collector_id} · {done_objects}/{total_objects} · {current_object}")
            self.floating_window.set_status(
                f"{verification_label} {current_fraction / 10:.1f}%",
                f"正在{verification_label} {collector_id} · {archive_date}",
                current_fraction,
                f"{done_objects}/{total_objects}",
            )
        elif phase == "verified":
            self.task_title.setText(f"验证通过 {archive_date}")
            self.task_detail.setText(f"{collector_id} · {total_objects} 个对象")
            self.task_progress.setValue(1000)
            self.floating_window.set_status(
                "完成", f"验证通过 {collector_id} · {archive_date}", 1000,
                "完成",
            )

    def _update_live_tree(self, payload: dict[str, Any]) -> None:
        collector_id = str(payload.get("collector_id") or "")
        archive_date = str(payload.get("archive_date") or "")
        day_key = f"{collector_id}/{archive_date}"
        day = next((value for value in self.catalog.days if value.key == day_key), None)
        day_item = self._tree_day_items.get(day_key)
        if day is None or day_item is None:
            return
        phase = str(payload.get("phase") or "")
        total_bytes = int(payload.get("bytes_total") or day.bytes_total)
        done_bytes = min(total_bytes, max(0, int(payload.get("bytes_done") or 0)))
        total_objects = int(payload.get("object_count") or day.object_count)
        done_objects = min(total_objects, max(0, int(payload.get("objects_done") or 0)))
        if phase == "syncing":
            day.bytes_local = done_bytes
            day.local_status = "partial"
            day_item.setText(3, bytes_text(done_bytes))
            self._set_tree_progress(
                day_item,
                int(done_bytes * 100 / total_bytes) if total_bytes else 0,
            )
            day_item.setText(5, "文件传输中")
            day_item.setForeground(5, self._status_color("syncing"))
            key = str(payload.get("current_object") or "")
            obj = next((value for value in day.objects if value.relative_key == key), None)
            file_item = self._tree_file_items.get((day_key, key))
            if obj is not None:
                current = int(payload.get("current_object_bytes") or obj.local_bytes)
                obj.local_bytes = min(obj.size_bytes, max(0, current))
            if obj is not None and file_item is not None:
                file_item.setText(3, bytes_text(obj.local_bytes))
                self._set_tree_progress(
                    file_item,
                    int(obj.local_bytes * 100 / obj.size_bytes) if obj.size_bytes else 0,
                )
                file_item.setText(5, "传输中")
                file_item.setForeground(5, self._status_color("syncing"))
        elif phase == "verifying":
            verification_level = str(payload.get("verification_level") or "deep")
            verification_label = "传输校验" if verification_level == "transport" else "完整校验"
            self._set_tree_progress(
                day_item,
                int(done_objects * 100 / total_objects) if total_objects else 0,
            )
            day_item.setText(5, f"{verification_label}中")
            day_item.setForeground(5, self._status_color("verifying"))
            key = str(payload.get("current_object") or "")
            file_item = self._tree_file_items.get((day_key, key))
            if file_item is not None:
                file_item.setText(5, "校验中")
                file_item.setForeground(5, self._status_color("verifying"))
        elif phase == "verified":
            verification_level = str(payload.get("verification_level") or "deep")
            verification_label = "传输校验" if verification_level == "transport" else "完整校验"
            day.local_status = "verified"
            day.local_verification_level = verification_level
            day.bytes_local = day.bytes_total
            day_item.setText(3, bytes_text(day.bytes_total))
            self._set_tree_progress(day_item, 100, verified=True)
            day_item.setText(5, f"{verification_label}通过")
            day_item.setIcon(0, self._icon("folder-check", "icon_success"))
            day_item.setForeground(5, self._status_color("verified"))
            for obj in day.objects:
                obj.local_bytes = obj.size_bytes
                file_item = self._tree_file_items.get((day_key, obj.relative_key))
                if file_item is not None:
                    file_item.setText(3, bytes_text(obj.size_bytes))
                    self._set_tree_progress(file_item, 100, verified=True)
                    file_item.setText(5, "通过")
                    file_item.setForeground(1, self._status_color("verified"))
                    file_item.setForeground(5, self._status_color("verified"))

    def _on_catalog_ready(self, catalog: ArchiveCatalog) -> None:
        self.catalog = catalog
        self._populate_tree(catalog)
        self._persist_catalog_async(catalog)

    def _persist_catalog_async(self, catalog: ArchiveCatalog) -> None:
        def save() -> None:
            try:
                self.state_store.save_catalog(catalog)
                # Both time-based alarms measure this: when the last successful
                # scan happened, and the newest archive day it found.
                self.notifier.record_scan(
                    catalog.scanned_at, [day.archive_date for day in catalog.days]
                )
            except Exception:
                LOGGER.exception("Unable to persist catalog cache")

        threading.Thread(target=save, name="smsi-save-cache", daemon=True).start()

    def _on_operation_done(self, operation: str, result: object) -> None:
        self.busy_operation = ""
        self._update_busy_state()
        if operation in {"scan", "auto_scan"} and isinstance(result, ArchiveCatalog):
            self._on_catalog_ready(result)
            self.connection_label.setText("已连接")
            self.task_title.setText("扫描完成")
            self.task_detail.setText(f"发现 {len(result.days)} 个归档日期")
            self.task_progress.setRange(0, 1000)
            self.task_progress.setValue(1000)
            self.state_store.event("info", "Ubuntu 文件列表扫描完成", f"{len(result.days)} 个日期")
            eligible = automatic_sync_days(result.days)
            if self.config.auto_sync_enabled and eligible:
                self._start_sync_days(eligible, "auto_sync")
                return
            self._update_floating_idle_state("扫描完成")
        elif operation == "test":
            count = int((result or {}).get("collectors") or 0) if isinstance(result, dict) else 0
            self.task_title.setText("连接正常")
            self.task_detail.setText(f"读取到 {count} 个采集服务器目录")
            self.task_progress.setRange(0, 1000)
            self.task_progress.setValue(1000)
            QMessageBox.information(self, "连接正常", f"已连接 Ubuntu，并可读取 {REMOTE_ROOT}。")
        elif operation in {"sync", "auto_sync", "verify"}:
            count = len(result) if isinstance(result, list) else 0
            title = "同步完成" if operation in {"sync", "auto_sync"} else "校验完成"
            self.task_title.setText(title)
            self.task_detail.setText(f"{count} 个归档日期验证通过")
            self.task_progress.setValue(1000)
            self.state_store.event("info", title, f"{count} 个日期")
            self.floating_window.set_status(
                title, f"{count} 个归档日期验证通过", 1000,
                "自动" if self.config.auto_sync_enabled else "完成",
            )
            QTimer.singleShot(500, lambda: self.start_scan(
                automatic=self.config.auto_sync_enabled
            ))
        QTimer.singleShot(3000, self._clear_finished_progress)

    def _clear_finished_progress(self) -> None:
        """A finished bar is worth a moment, not the rest of the day.

        Left at 100% it kept reading as a task about to finish, hours after the
        last one had.  Only the idle case clears, so the rescan that follows a
        sync is never zeroed out from under itself.
        """
        if self.busy_operation not in RUNNING_OPERATIONS:
            self.task_progress.setValue(0)

    def _on_operation_error(self, operation: str, message: str) -> None:
        self.busy_operation = ""
        self._update_busy_state()
        cancelled = "取消" in message
        self.task_title.setText("已取消" if cancelled else "操作失败")
        self.task_detail.setText(message)
        # A failure is not progress: the second of these two lines used to be a
        # repeat of the first, which left the bar wherever the task stopped.
        self.task_progress.setRange(0, 1000)
        self.task_progress.setValue(0)
        self.state_store.event("warning" if cancelled else "error", self.task_title.text(), message)
        automatic = operation in {"auto_scan", "auto_sync"}
        if not cancelled and not automatic:
            QMessageBox.critical(self, "操作失败", message)
        elif not cancelled and automatic and self.tray_icon is not None:
            self.tray_icon.showMessage(
                "自动同步失败", message, QSystemTrayIcon.MessageIcon.Warning, 6000
            )
        self.floating_window.set_status(
            "已取消" if cancelled else "异常",
            message, 0,
            "自动" if self.config.auto_sync_enabled else "注意",
        )
        self._update_tray_state()
        if cancelled:
            QTimer.singleShot(500, self.start_scan)

    def _update_busy_state(self) -> None:
        busy = bool(self.busy_operation)
        self.refresh_button.setEnabled(not busy)
        self.sync_button.setEnabled(not busy)
        self.verify_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self.test_button.setEnabled(not busy)
        self.cancel_button.setEnabled(
            self.busy_operation in RUNNING_OPERATIONS - {"test"}
        )
        self._update_tray_state()

    def _update_action_state(self) -> None:
        if self.busy_operation:
            return
        self.verify_button.setEnabled(any(
            day.local_status in {"verified", "local_unverified", "verification_failed"}
            for day in self._selected_days(default_all=False)
        ))

    def _status_color(self, status: str) -> QColor:
        """The colour a status word is painted in, from the active palette.

        Tree item foregrounds are painted, not styled: a stylesheet cannot reach
        them, so these are the one set of colours that has to be looked up by
        hand on every render.
        """
        colours = self.theme.palette
        if status == "verified":
            return QColor(colours.success)
        if status in {"ready", "syncing", "verifying"}:
            return QColor(colours.accent)
        if status in {"remote_running", "waiting_manifest", "waiting_verification", "partial"}:
            return QColor(colours.warning)
        if status in {
            "remote_failed",
            "invalid",
            "local_unverified",
            "verification_failed",
            "manifest_changed",
            "error",
        }:
            return QColor(colours.danger)
        return QColor(colours.muted)

    def _day_display_status(self, day: ArchiveDay) -> tuple[str, str]:
        if day.remote_status == "ready":
            return "verified", "完整校验通过"
        return day.remote_status, STATUS_LABELS.get(
            day.remote_status, day.remote_status
        )

    def _populate_tree(self, catalog: ArchiveCatalog) -> None:
        expanded_days = {
            key for key, item in self._tree_day_items.items() if item.isExpanded()
        }
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            self._tree_day_items.clear()
            self._tree_file_items.clear()
            root = QTreeWidgetItem([f"Ubuntu  {catalog.remote_root}", "", "", "", "", "", ""])
            root.setData(0, NODE_ROLE, {"kind": "root"})
            root.setIcon(0, self._icon("server", "accent"))
            self.tree.addTopLevelItem(root)
            groups: dict[str, list[ArchiveDay]] = defaultdict(list)
            for day in catalog.days:
                groups[day.collector_id].append(day)
            for collector_id in sorted(groups, key=str.casefold):
                days = sorted(groups[collector_id], key=lambda value: value.archive_date, reverse=True)
                collector = QTreeWidgetItem(root, [f"collector={collector_id}"])
                collector.setData(0, NODE_ROLE, {"kind": "collector", "collector_id": collector_id})
                collector.setIcon(0, self._icon("database"))
                for day in days:
                    status_code, status_label = self._day_display_status(day)
                    if day.local_status == "verified":
                        verification = (
                            "传输校验通过"
                            if day.local_verification_level == "transport"
                            else "完整校验通过"
                        )
                        verification_code = "verified"
                    elif day.local_status == "verification_failed":
                        # Never let a recorded failure read as "not finished":
                        # the remote side is fine, so status_code would paint
                        # this column green.
                        verification = "校验失败"
                        verification_code = "verification_failed"
                    else:
                        verification = "未完成"
                        verification_code = status_code
                    item = QTreeWidgetItem(collector, [
                        f"date={day.archive_date}",
                        status_label,
                        bytes_text(day.bytes_total),
                        bytes_text(day.bytes_local),
                        "",
                        verification,
                        archive_content_text(day),
                    ])
                    item.setData(0, NODE_ROLE, {
                        "kind": "day", "day_key": day.key, "files_loaded": False
                    })
                    item.setIcon(0, self._icon(
                        "folder-check" if day.local_status == "verified" else "folder",
                        "icon_success" if day.local_status == "verified" else "icon",
                    ))
                    item.setForeground(1, self._status_color(status_code))
                    item.setForeground(5, self._status_color(verification_code))
                    self._set_tree_progress(
                        item,
                        int(day.bytes_local * 100 / day.bytes_total) if day.bytes_total else 0,
                        verified=day.local_status == "verified",
                    )
                    item.setToolTip(1, day.remote_detail)
                    item.setToolTip(5, day.local_detail)
                    item.setToolTip(6, self._archive_summary_detail(day))
                    self._tree_day_items[day.key] = item
                    if day.manifest_raw or day.objects:
                        placeholder = QTreeWidgetItem(item, ["展开查看文件"])
                        placeholder.setData(0, NODE_ROLE, {
                            "kind": "placeholder", "day_key": day.key
                        })
                        placeholder.setForeground(0, self._status_color("unknown"))
                    if day.key in expanded_days:
                        item.setExpanded(True)
                        self._load_day_files(item)
            root.setExpanded(True)
            for index in range(root.childCount()):
                root.child(index).setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_summary()
        self._update_action_state()

    def _on_tree_item_expanded(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, NODE_ROLE) or {}
        if data.get("kind") == "day":
            self._load_day_files(item)

    def _load_day_files(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, NODE_ROLE) or {}
        if data.get("kind") != "day" or data.get("files_loaded"):
            return
        day_key = str(data.get("day_key") or "")
        day = next((value for value in self.catalog.days if value.key == day_key), None)
        if day is None:
            return
        while item.childCount():
            item.takeChild(0)
        self.tree.setUpdatesEnabled(False)
        try:
            self._add_day_files(item, day)
            data["files_loaded"] = True
            item.setData(0, NODE_ROLE, data)
        finally:
            self.tree.setUpdatesEnabled(True)

    def _add_day_files(self, parent: QTreeWidgetItem, day: ArchiveDay) -> None:
        directory_items: dict[str, QTreeWidgetItem] = {}
        if day.manifest_raw:
            manifest = QTreeWidgetItem(parent, [
                "manifest.json", "已发布", bytes_text(len(day.manifest_raw)), "--", "--", "协议有效", "--"
            ])
            manifest.setIcon(0, self._icon("file-check", "accent"))
            manifest.setData(0, NODE_ROLE, {"kind": "file", "day_key": day.key, "path": "manifest.json"})
            manifest.setForeground(5, self._status_color("verified"))
            manifest.setToolTip(5, f"SHA-256: {day.manifest_sha256}")
        for obj in sorted(day.objects, key=lambda value: value.relative_key.casefold()):
            parts = PurePosixPath(obj.relative_key).parts[1:]
            current_parent = parent
            current_path = ""
            for directory in parts[:-1]:
                current_path = f"{current_path}/{directory}".strip("/")
                if current_path not in directory_items:
                    directory_item = QTreeWidgetItem(current_parent, [directory])
                    directory_item.setIcon(0, self._icon("folder"))
                    directory_item.setData(0, NODE_ROLE, {
                        "kind": "directory", "day_key": day.key, "path": current_path
                    })
                    directory_items[current_path] = directory_item
                current_parent = directory_items[current_path]
            local_state = (
                "verified" if day.local_status == "verified"
                else "partial" if obj.local_bytes else "missing"
            )
            file_item = QTreeWidgetItem(current_parent, [
                parts[-1],
                "已发布",
                bytes_text(obj.size_bytes),
                bytes_text(obj.local_bytes),
                "",
                "通过" if day.local_status == "verified" else "待校验",
                "--",
            ])
            file_item.setIcon(0, self._icon(
                "table-properties" if obj.item_format == "parquet" else "file-text",
            ))
            file_item.setData(0, NODE_ROLE, {
                "kind": "file", "day_key": day.key, "path": obj.relative_key
            })
            file_item.setForeground(1, self._status_color("verified"))
            file_item.setForeground(5, self._status_color(local_state))
            self._set_tree_progress(
                file_item,
                int(obj.local_bytes * 100 / obj.size_bytes) if obj.size_bytes else 0,
                verified=day.local_status == "verified",
            )
            file_item.setToolTip(5, f"SHA-256: {obj.sha256}\n行数: {obj.row_count:,}")
            self._tree_file_items[(day.key, obj.relative_key)] = file_item

    def _set_tree_progress(
        self, item: QTreeWidgetItem, percent: int, *, verified: bool = False
    ) -> None:
        progress = self.tree.itemWidget(item, 4)
        if not isinstance(progress, TreeProgressWidget):
            progress = TreeProgressWidget()
            self.tree.setItemWidget(item, 4, progress)
        progress.set_progress(percent, verified=verified)

    def _update_summary(self) -> None:
        ready = [day for day in self.catalog.days if day.eligible]
        verified = [day for day in self.catalog.days if day.local_status == "verified"]
        pending = sum(max(0, day.bytes_total - day.bytes_local) for day in ready)
        self.ready_label.setText(str(len(ready)))
        self.pending_label.setText(bytes_text(pending))
        self.verified_label.setText(str(len(verified)))
        continuity, gaps = archive_continuity(self.catalog.days)
        self.continuity_label.setText(f"{continuity} 天")
        self.gap_label.setText(str(gaps))
        self._refresh_scan_age()
        self._render_disk_metric()
        if not self.busy_operation:
            self.task_title.setText("空闲")
            self.task_progress.setRange(0, 1000)
            self.task_progress.setValue(0)

    def _refresh_scan_age(self) -> None:
        scanned = self.catalog.scanned_at
        self.last_scan_label.setText(age_text(scanned))
        self.last_scan_label.setToolTip(
            f"上次成功扫描 Ubuntu 的时间：{timestamp_text(scanned)}"
            if scanned else "还没有成功扫描过 Ubuntu"
        )

    def _render_disk_metric(self) -> None:
        """Free space belongs on the default page, not buried in settings.

        A volume filling up is the one fault that gives warning before it stops
        the archive, so the warning has to be somewhere the user already looks.
        One reading per catalogue render is enough: the number only moves when
        something was downloaded.
        """
        status = disk_status(self.config.local_root, self.config.minimum_free_gib)
        if not status["ok"]:
            self.disk_label.setText("无法读取")
            self.disk_label.setStyleSheet(f"color: {self.theme.palette.danger}")
            self.disk_label.setToolTip(f"无法读取归档盘：{status['error']}")
            return
        self.disk_label.setText(bytes_text(status["free_bytes"]))
        self.disk_label.setStyleSheet(
            f"color: {self.theme.palette.danger}" if status["low"] else ""
        )
        self.disk_label.setToolTip(
            f"归档盘共 {bytes_text(status['total_bytes'])}，"
            f"保留下限 {bytes_text(status['reserve_bytes'])}；"
            "可用空间低于保留下限时同步会提前停下"
        )

    def _archive_summary_detail(self, day: ArchiveDay) -> str:
        summary = day.summary
        kinds = summary.kind_counts
        coverage = (
            f"{timestamp_text(summary.window_start)} → {timestamp_text(summary.window_end)}"
            if summary.window_start and summary.window_end
            else "--"
        )
        return "\n".join((
            f"归档文件：{day.object_count:,}",
            f"归档总行数：{day.row_count:,}",
            f"业务表：{summary.business_table_count:,}",
            (
                "对象分类："
                f"business {kinds.get('business', 0):,} / "
                f"control {kinds.get('control', 0):,} / "
                f"evidence {kinds.get('evidence', 0):,} / "
                f"raw {kinds.get('raw', 0):,}"
            ),
            f"采集批次：{summary.batch_count:,}",
            f"账本记录：{summary.ledger_record_count:,}",
            f"未资格化批次：{summary.unqualified_batch_count:,}",
            f"时间覆盖：{coverage}",
            f"Google Drive 读回：{summary.remote_verified_count:,}/{day.object_count:,}",
            f"归档生成：{timestamp_text(summary.created_at)}",
            f"Ubuntu 验证：{timestamp_text(summary.ubuntu_verified_at)}",
            f"Windows 验证：{timestamp_text(summary.windows_verified_at)}",
        ))

    def _show_item_detail(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, NODE_ROLE) or {}
        day_key = data.get("day_key")
        day = next((value for value in self.catalog.days if value.key == day_key), None)
        if day is None:
            return
        path = str(data.get("path") or "")
        obj = next((value for value in day.objects if value.relative_key == path), None)
        if obj is not None:
            detail = (
                f"路径：{obj.relative_key}\n"
                f"类型：{obj.kind or obj.item_format}\n"
                f"远端大小：{bytes_text(obj.size_bytes)}\n"
                f"本地大小：{bytes_text(obj.local_bytes)}\n"
                f"行数：{obj.row_count:,}\n"
                f"SHA-256：{obj.sha256}\n"
                f"Schema：{obj.schema_sha256 or '--'}"
            )
        else:
            detail = (
                f"采集服务器：{day.collector_id}\n"
                f"归档日期：{day.archive_date}\n"
                f"Ubuntu：{'完整校验通过' if day.remote_status == 'ready' else STATUS_LABELS.get(day.remote_status, day.remote_status)}\n"
                f"Windows：{STATUS_LABELS.get(day.local_status, day.local_status)}"
                f"{' · ' + ('传输校验' if day.local_verification_level == 'transport' else '完整校验') if day.local_status == 'verified' else ''}\n"
                f"对象：{day.object_count:,}\n"
                f"数据量：{bytes_text(day.bytes_total)}\n"
                f"Manifest SHA-256：{day.manifest_sha256 or '--'}\n\n"
                f"{day.remote_detail}\n{day.local_detail}\n\n"
                f"归档内容\n{self._archive_summary_detail(day)}"
            )
        QMessageBox.information(self, "归档详情", detail)

    def closeEvent(self, event) -> None:
        if self._exit_requested:
            self.alert_worker.stop()
            self.floating_window.close()
            event.accept()
            return
        if self.tray_icon is not None:
            event.ignore()
            self._hide_to_tray()
            return
        if self.busy_operation in {"sync", "auto_sync", "verify"}:
            answer = QMessageBox.question(
                self,
                "任务正在运行",
                "关闭程序会取消当前任务，未完成文件会保留以便下次继续。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel_event.set()
        self.alert_worker.stop()
        self.floating_window.close()
        event.accept()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._update_tray_state)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        QTimer.singleShot(0, self._update_tray_state)
