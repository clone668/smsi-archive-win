from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QLabel,
    QStyle,
    QStyleOptionFrame,
)

from smsi_archive_sync.config import ConfigStore
from smsi_archive_sync.models import ArchiveCatalog, ArchiveDay, ArchiveSummary, RemoteObject
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.style import APP_STYLE
from smsi_archive_sync.ui import (
    MainWindow,
    TreeProgressWidget,
    age_text,
    archive_continuity,
    automatic_sync_days,
    compact_count,
    timestamp_text,
)


def test_main_window_renders(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    store = ConfigStore(tmp_path / "config")
    window = MainWindow(store, StateStore(tmp_path / "state"), QIcon())
    window.show()
    app.processEvents()
    assert window.windowTitle() == "SMSI 归档同步"
    assert window.tree.topLevelItemCount() == 1
    assert window.tree.topLevelItem(0).text(0).startswith("Ubuntu")
    assert window.minimumWidth() >= 1000
    assert window.size() == window.minimumSize()
    assert window.floating_window.isVisible() is False
    assert window.findChild(QFrame, "sidebar") is None
    assert window.sync_nav.objectName() == "topTab"
    assert window.settings_nav.objectName() == "topTab"
    assert window.sync_nav.geometry().top() < window.pages.geometry().top()
    if os.getenv("SMSI_UI_PAGE") == "settings":
        window._set_page(1)
        app.processEvents()
    assert window.grab().isNull() is False
    screenshot = os.getenv("SMSI_UI_SCREENSHOT")
    if screenshot:
        window.grab().save(screenshot)
    window.close()


def test_timestamp_text_removes_subseconds() -> None:
    assert timestamp_text("2026-08-10T13:50:37.923839Z") == (
        "2026-08-10 13:50:37 UTC"
    )


def test_top_tabs_switch_pages_exclusively(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    window._set_page(1)
    app.processEvents()
    assert window.pages.currentIndex() == 1
    assert window.settings_nav.isChecked() is True
    assert window.sync_nav.isChecked() is False

    window._set_page(0)
    app.processEvents()
    assert window.pages.currentIndex() == 0
    assert window.sync_nav.isChecked() is True
    assert window.settings_nav.isChecked() is False
    window._exit_requested = True
    window.close()


def test_day_files_are_loaded_only_when_expanded(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    day = ArchiveDay(
        collector_id="tencent-paper",
        archive_date="2026-08-09",
        remote_status="ready",
        object_count=1,
        objects=[RemoteObject(
            relative_key="date=2026-08-09/control/example.parquet",
            size_bytes=10,
            sha256="0" * 64,
        )],
    )
    day.set_manifest_raw(b"{}")
    window.catalog = ArchiveCatalog(days=[day])
    window._populate_tree(window.catalog)

    day_item = window._tree_day_items[day.key]
    assert day_item.childCount() == 1
    assert (day.key, day.objects[0].relative_key) not in window._tree_file_items

    day_item.setExpanded(True)
    app.processEvents()
    assert (day.key, day.objects[0].relative_key) in window._tree_file_items
    window._exit_requested = True
    window.close()


def test_tree_uses_progress_bars_for_days_and_files(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    obj = RemoteObject(
        relative_key="date=2026-08-09/control/example.parquet",
        size_bytes=10,
        sha256="0" * 64,
        local_bytes=4,
    )
    day = ArchiveDay(
        collector_id="tencent-paper",
        archive_date="2026-08-09",
        remote_status="ready",
        local_status="partial",
        object_count=1,
        row_count=4_150_566,
        bytes_total=10,
        bytes_local=4,
        objects=[obj],
        summary=ArchiveSummary(
            business_table_count=28,
            batch_count=133_120,
            ledger_record_count=1_305_107,
            kind_counts={"business": 28, "control": 1, "evidence": 24, "raw": 24},
            remote_verified_count=1,
        ),
    )
    day.set_manifest_raw(b"{}")
    window.catalog = ArchiveCatalog(days=[day])
    window._populate_tree(window.catalog)
    day_item = window._tree_day_items[day.key]
    day_progress = window.tree.itemWidget(day_item, 4)
    assert isinstance(day_progress, TreeProgressWidget)
    assert day_progress.bar.value() == 40
    assert day_progress.label.property("onFill") is False
    assert day_item.text(6) == "28 业务表 · 1.31M 业务记录"
    assert "采集批次：133,120" in day_item.toolTip(6)
    assert "业务表：28" in day_item.toolTip(6)

    day_item.setExpanded(True)
    app.processEvents()
    file_item = window._tree_file_items[(day.key, obj.relative_key)]
    file_progress = window.tree.itemWidget(file_item, 4)
    assert isinstance(file_progress, TreeProgressWidget)
    assert file_progress.bar.value() == 40
    window._exit_requested = True
    window.close()


def test_live_verification_updates_day_icon_immediately(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    day = ArchiveDay(
        collector_id="tencent-paper",
        archive_date="2026-08-09",
        remote_status="ready",
        local_status="partial",
        object_count=1,
        bytes_total=10,
        bytes_local=10,
        objects=[RemoteObject(
            relative_key="date=2026-08-09/control/example.parquet",
            size_bytes=10,
            sha256="0" * 64,
        )],
    )
    window.catalog = ArchiveCatalog(days=[day])
    window._populate_tree(window.catalog)
    day_item = window._tree_day_items[day.key]
    initial_icon_key = day_item.icon(0).cacheKey()

    window._update_live_tree({
        "phase": "verified",
        "verification_level": "transport",
        "collector_id": day.collector_id,
        "archive_date": day.archive_date,
        "object_count": day.object_count,
        "objects_done": day.object_count,
        "bytes_total": day.bytes_total,
        "bytes_done": day.bytes_total,
    })
    app.processEvents()

    assert day.local_status == "verified"
    assert day_item.text(1) == "完整校验通过"
    assert day_item.text(5) == "传输校验通过"
    assert day_item.icon(0).cacheKey() != initial_icon_key
    progress = window.tree.itemWidget(day_item, 4)
    assert isinstance(progress, TreeProgressWidget)
    assert progress.bar.value() == 100
    assert progress.bar.property("progressState") == "verified"
    assert progress.label.property("onFill") is True
    window._exit_requested = True
    window.close()


def test_auto_scan_starts_only_eligible_days(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    eligible = ArchiveDay(
        collector_id="tencent-paper",
        archive_date=(date.today() - timedelta(days=1)).isoformat(),
        remote_status="ready",
    )
    blocked = ArchiveDay(
        collector_id="tencent-paper",
        archive_date=date.today().isoformat(),
        remote_status="remote_running",
    )
    started = []
    monkeypatch.setattr(
        window,
        "_start_sync_days",
        lambda days, operation: started.append((days, operation)) or True,
    )
    window.config.auto_sync_enabled = True
    window._on_operation_done("auto_scan", ArchiveCatalog(days=[eligible, blocked]))

    assert len(started) == 1
    assert started[0][0] == [eligible]
    assert started[0][1] == "auto_sync"
    assert window.floating_window.width() <= 180
    assert window.floating_window.height() <= 90
    window._exit_requested = True
    window.close()
    app.processEvents()


def test_continuity_and_automatic_sync_scope() -> None:
    days = [
        ArchiveDay("paper", "2026-08-07", "ready", local_status="verified"),
        ArchiveDay("paper", "2026-08-08", "ready", local_status="verified"),
        ArchiveDay("paper", "2026-08-09", "ready", local_status="missing"),
        ArchiveDay("report", "2026-08-07", "ready", local_status="verified"),
        ArchiveDay("report", "2026-08-09", "ready", local_status="missing"),
    ]

    continuity, gaps = archive_continuity(days)
    automatic = automatic_sync_days(
        [
            *days,
            ArchiveDay("paper", "2026-07-01", "ready", local_status="missing"),
        ],
        today=date(2026, 8, 10),
    )

    assert continuity == 1
    assert gaps == 1
    assert {day.key for day in automatic} == {
        "paper/2026-08-09",
        "report/2026-08-09",
    }


def test_saving_settings_keeps_the_stored_alert_token_and_wakes_alerting(
    tmp_path, monkeypatch
) -> None:
    """The token is the one setting a careless save could silently destroy."""
    token = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
    app = QApplication.instance() or QApplication([])
    store = ConfigStore(tmp_path / "config")
    config = store.load()
    config.set_alert_token(token)
    config.alert_chat_id = "-1001234567890"
    config.alert_enabled = True
    store.save(config)
    window = MainWindow(store, StateStore(tmp_path / "state"), QIcon())
    app.processEvents()

    assert window.alert_enabled_input.currentData() is True
    assert window.alert_chat_input.text() == "-1001234567890"
    # Never shown back, so an empty box has to mean "keep the saved one".
    assert window.alert_token_input.text() == ""

    woke: list[bool] = []
    monkeypatch.setattr(window.alert_worker, "wake", lambda: woke.append(True))
    window.alert_stale_input.setValue(12)
    window.save_settings()

    saved = store.load()
    assert saved.alert_token() == token
    assert saved.alert_stale_hours == 12
    # A new threshold that waits for the next poll interval is a new threshold
    # the user does not yet have.
    assert woke == [True]
    assert "已启用" in window.alert_health_label.text()

    window._exit_requested = True
    window.close()


def test_tray_actions_follow_window_and_sync_state(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    window.show_window_action = QAction("显示主窗口", window)
    window.auto_sync_action = QAction("自动同步", window)
    window.auto_sync_action.setCheckable(True)
    window.stop_sync_action = QAction("停止同步", window)
    window.floating_window_action = QAction("显示悬浮窗", window)
    window.floating_window_action.setCheckable(True)
    monkeypatch.setattr(window.config, "validate", lambda **_kwargs: [])
    window._startup_loading = False

    window.show()
    app.processEvents()
    window.config.auto_sync_enabled = False
    window.busy_operation = ""
    window._update_tray_state()
    assert window.show_window_action.isEnabled() is False
    assert window.auto_sync_action.isEnabled() is True
    assert window.stop_sync_action.isEnabled() is False

    window.busy_operation = "sync"
    window._update_tray_state()
    assert window.stop_sync_action.isEnabled() is True

    window.config.auto_sync_enabled = True
    window.busy_operation = "auto_sync"
    window.cancel_event.clear()
    window.stop_all_sync()
    assert window.config.auto_sync_enabled is False
    assert window.cancel_event.is_set() is True

    window.config.floating_window_enabled = True
    window._update_tray_state()
    assert window.floating_window_action.isChecked() is True
    assert window.floating_window_action.text() == "隐藏悬浮窗"

    menu_positions = []
    window.floating_window.menu_requested.connect(menu_positions.append)
    window.floating_window.panel.customContextMenuRequested.emit(QPoint(5, 5))
    assert len(menu_positions) == 1
    window._exit_requested = True
    window.close()


def _shown_window(tmp_path, page: int = 0):
    """A styled, shown window: every layout invariant below depends on the QSS."""
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow(
        ConfigStore(tmp_path / "config"),
        StateStore(tmp_path / "state"),
        QIcon(),
    )
    window.show()
    window._set_page(page)
    app.processEvents()
    return app, window


def test_tree_header_never_clips_its_own_headings(tmp_path) -> None:
    """A pinned width has to be at least as wide as the word it must show.

    Two of the seven were not, and "Ubuntu 状态" reached the user as
    "Ubuntu 状".  The assertion is the invariant rather than the pinned numbers:
    the heading plus the 8px of section padding on each side has to fit.
    """
    _app, window = _shown_window(tmp_path)
    header = window.tree.header()
    header.ensurePolished()
    metrics = QFontMetrics(header.font())
    for column in range(window.tree.columnCount()):
        heading = window.tree.headerItem().text(column)
        assert header.sectionSize(column) >= metrics.horizontalAdvance(heading) + 16, heading
    # The name column holds the unbounded content, so it takes the spare width.
    # Letting the last section stretch instead silently voids its pinned width.
    assert header.stretchLastSection() is False
    window._exit_requested = True
    window.close()


def _first_fields(window) -> list[tuple[str, int]]:
    """The first input of every settings card, with the card's own title."""
    rows = []
    for frame in window.findChildren(QFrame, "settingsSection"):
        form = frame.findChildren(QFormLayout)[0]
        title = frame.findChildren(QLabel)[0].text()
        rows.append((title, form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget().x()))
    return rows


def test_settings_cards_share_one_label_column(tmp_path) -> None:
    """Four cards, one vertical line for the inputs to start on.

    A form layout measures its label column from its own longest label, so the
    card whose labels were shortest put its boxes tens of pixels left of the
    card underneath it.
    """
    _app, window = _shown_window(tmp_path, page=1)
    rows = _first_fields(window)
    assert len(rows) >= 4
    assert len({x for _title, x in rows}) == 1, rows
    window._exit_requested = True
    window.close()


def test_every_settings_field_is_the_same_height(tmp_path) -> None:
    """A spin box asks for 3px more than a line edit and has to be capped.

    Styling the box without capping it left 端口 and 磁盘保留空间 standing taller
    than every other row on the page.
    """
    _app, window = _shown_window(tmp_path, page=1)
    heights = {
        "host": window.host_input.height(),
        "port": window.port_input.height(),
        "reserve": window.reserve_input.height(),
        "bandwidth": window.bandwidth_input.height(),
    }
    assert len(set(heights.values())) == 1, heights
    window._exit_requested = True
    window.close()


def test_keyboard_focus_costs_no_geometry(tmp_path) -> None:
    """The accent ring is two pixels the layout must not pay for.

    A focus border that is not compensated in padding and min-height grows the
    widget, which pushes the form row taller and steps the text sideways - so
    tabbing through the page makes it twitch.
    """
    app, window = _shown_window(tmp_path, page=1)
    for name in ("host_input", "port_input", "bandwidth_input", "save_button"):
        widget = getattr(window, name)
        before = (widget.geometry(), widget.sizeHint())
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        assert (widget.geometry(), widget.sizeHint()) == before, name
        widget.clearFocus()
        app.processEvents()

    def text_origin() -> int:
        option = QStyleOptionFrame()
        option.initFrom(window.host_input)
        return window.host_input.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents, option, window.host_input
        ).x()

    unfocused = text_origin()
    window.host_input.setFocus(Qt.FocusReason.TabFocusReason)
    app.processEvents()
    assert text_origin() == unfocused
    window._exit_requested = True
    window.close()


def test_status_bar_reports_auto_sync_instead_of_the_task_band(tmp_path) -> None:
    """The bar used to repeat the sentence already on screen two rows above.

    Whether unattended syncing is armed was shown nowhere in the window, only in
    the tray menu, so that is what earns the permanent slot.
    """
    _app, window = _shown_window(tmp_path)
    window.config.auto_sync_enabled = True
    window._update_auto_sync_chip()
    assert window.auto_sync_chip.property("state") == "on"
    assert "已开启" in window.auto_sync_chip.text()
    window.config.auto_sync_enabled = False
    window._update_auto_sync_chip()
    assert window.auto_sync_chip.property("state") == "off"
    window._exit_requested = True
    window.close()


def test_unconfigured_client_says_so_once(tmp_path) -> None:
    """Not twice, and not in a message that never times out."""
    _app, window = _shown_window(tmp_path)
    window._on_cached_catalog_ready(None)
    assert window.task_detail.text() == "请先在设置中完成 Ubuntu 连接"
    assert window.statusBar().currentMessage() == ""
    window._exit_requested = True
    window.close()


def test_finished_progress_clears_only_while_idle(tmp_path) -> None:
    """A bar left at 100% reads as a task about to finish, hours after one did.

    It clears a moment after the task ends - but only if nothing has started in
    the meantime, so the rescan that follows a sync is never zeroed out from
    under itself.
    """
    _app, window = _shown_window(tmp_path)
    window.task_progress.setValue(1000)
    window.busy_operation = ""
    window._clear_finished_progress()
    assert window.task_progress.value() == 0

    window.task_progress.setValue(400)
    window.busy_operation = "sync"
    window._clear_finished_progress()
    assert window.task_progress.value() == 400
    window.busy_operation = ""
    window._exit_requested = True
    window.close()


def test_compact_count_keeps_a_column_of_magnitudes_aligned() -> None:
    """Stripping the trailing zero printed 1.7M directly above 1.58M."""
    assert compact_count(1_700_000) == "1.70M"
    assert compact_count(1_584_912) == "1.58M"
    assert compact_count(12_345_678) == "12.3M"
    assert compact_count(999) == "999"
    assert compact_count(None) == "0"


def test_scan_age_answers_the_question_that_was_asked() -> None:
    """How long ago, not when: the reader wants to know if it is still scanning.

    A full UTC stamp was three times the width of every other reading in the
    strip and still needed a subtraction in the reader's head on a UTC+08 box.
    """
    now = datetime.now(timezone.utc)
    assert age_text(now.isoformat()) == "刚刚"
    assert age_text((now - timedelta(minutes=7)).isoformat()) == "7 分钟前"
    assert age_text((now - timedelta(hours=5)).isoformat()) == "5 小时前"
    assert age_text((now - timedelta(days=3)).isoformat()) == "3 天前"
    assert age_text("") == "--"
    # A stamp Qt cannot parse is still worth showing verbatim.
    assert age_text("不是时间") == "不是时间"


def test_scan_age_keeps_the_exact_stamp_on_the_tooltip(tmp_path) -> None:
    """The reading is relative; the stamp it was derived from stays reachable."""
    _app, window = _shown_window(tmp_path)
    window.catalog = ArchiveCatalog(days=[], scanned_at="2026-09-05T22:25:42.123456Z")
    window._refresh_scan_age()
    assert window.last_scan_label.text() != "2026-09-05 22:25:42 UTC"
    assert "2026-09-05 22:25:42 UTC" in window.last_scan_label.toolTip()
    window._exit_requested = True
    window.close()




