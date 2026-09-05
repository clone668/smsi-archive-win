from __future__ import annotations

import os
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QFrame

from smsi_archive_sync.config import ConfigStore
from smsi_archive_sync.models import ArchiveCatalog, ArchiveDay, ArchiveSummary, RemoteObject
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.style import APP_STYLE
from smsi_archive_sync.ui import (
    MainWindow,
    TreeProgressWidget,
    archive_continuity,
    automatic_sync_days,
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
