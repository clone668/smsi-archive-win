from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QColor, QFontMetrics, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QLabel,
    QMenu,
    QStyle,
    QStyleOptionFrame,
)

from smsi_archive_sync.config import AppConfig, ConfigStore
from smsi_archive_sync.models import ArchiveCatalog, ArchiveDay, ArchiveSummary, RemoteObject
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.style import apply_theme
from smsi_archive_sync.themes import (
    DEFAULT_THEME_NAME,
    STANDARD_THEME,
    THEMES,
    build_qss,
    normalize_theme_name,
)
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
    apply_theme(app, STANDARD_THEME)
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


def test_supplement_progress_and_failure_do_not_change_native_state(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(ConfigStore(tmp_path / "config"), StateStore(tmp_path / "state"), QIcon())
    day = ArchiveDay(collector_id="tencent-paper", archive_date="2026-10-12",
        remote_status="ready", local_status="verified", bytes_total=100, bytes_local=100,
        dependency_summary={"state": "verified", "dependency_status": "partial"})
    window.catalog = ArchiveCatalog(days=[day])
    window._populate_tree(window.catalog)
    assert window._tree_day_items[day.key].text(6).startswith("补包已验证 · 材料缺项")
    assert "材料仍有缺项" in window._tree_day_items[day.key].toolTip(6)
    day.dependency_summary = {"state": "ready", "catalog_key": "test", "bytes_total": 1024}
    window._update_summary()
    assert window.pending_label.text() == "1.0 KiB"
    window._on_progress({"phase": "supplement", "collector_id": day.collector_id,
        "archive_date": day.archive_date, "bytes_done": 10, "bytes_total": 20})
    assert window.task_progress.value() == 500
    assert day.local_status == "verified" and day.bytes_local == 100
    day.dependency_summary = {"state": "error", "error": "supplement transfer failed"}
    window._on_operation_done("auto_sync", [{"collector_id": day.collector_id,
        "archive_date": day.archive_date, "dependency_summary": day.dependency_summary}])
    assert "补充包有失败" in window.task_title.text()
    assert "supplement transfer failed" in window._tree_day_items[day.key].toolTip(6)
    assert day.local_status == "verified"
    window._exit_requested = True
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


def _shown_window(tmp_path, page: int = 0, theme: str = DEFAULT_THEME_NAME):
    """A styled, shown window: every layout invariant below depends on the QSS.

    The theme is stored before the window is built, because a window measures its
    tree header and its label column while building and would otherwise take
    those measurements against the wrong font.
    """
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    # Whatever the previous test left on the application, including a dark
    # palette: these invariants must not depend on the order tests run in.
    apply_theme(app, STANDARD_THEME)
    store = ConfigStore(tmp_path / "config")
    if theme != DEFAULT_THEME_NAME:
        config = store.load()
        config.theme = theme
        store.save(config)
    window = MainWindow(store, StateStore(tmp_path / "state"), QIcon())
    window.show()
    window._set_page(page)
    app.processEvents()
    return app, window


def _release(window) -> None:
    """Close a test window and hand the application back its light palette."""
    window._exit_requested = True
    window.close()
    apply_theme(QApplication.instance(), STANDARD_THEME)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_tree_header_never_clips_its_own_headings(tmp_path, theme) -> None:
    """A pinned width has to be at least as wide as the word it must show.

    Two of the seven were not, and "Ubuntu 状态" reached the user as
    "Ubuntu 状".  The assertion is the invariant rather than the pinned numbers:
    the heading plus the 8px of section padding on each side has to fit.  Run for
    every theme, because a theme carries both the pinned widths and the point
    size the headings are drawn at, and it is free to get the pair wrong.
    """
    _app, window = _shown_window(tmp_path, theme=theme)
    header = window.tree.header()
    header.ensurePolished()
    metrics = QFontMetrics(header.font())
    for column in range(window.tree.columnCount()):
        heading = window.tree.headerItem().text(column)
        assert header.sectionSize(column) >= metrics.horizontalAdvance(heading) + 16, heading
    # The name column holds the unbounded content, so it takes the spare width.
    # Letting the last section stretch instead silently voids its pinned width.
    assert header.stretchLastSection() is False
    _release(window)


def _first_fields(window) -> list[tuple[str, int]]:
    """The first input of every settings card, with the card's own title."""
    rows = []
    for frame in window.findChildren(QFrame, "settingsSection"):
        form = frame.findChildren(QFormLayout)[0]
        title = frame.findChildren(QLabel)[0].text()
        rows.append((title, form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget().x()))
    return rows


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_settings_cards_share_one_label_column(tmp_path, theme) -> None:
    """Four cards, one vertical line for the inputs to start on.

    A form layout measures its label column from its own longest label, so the
    card whose labels were shortest put its boxes tens of pixels left of the
    card underneath it.  A theme that stacks labels above their fields instead
    satisfies this the other way: every field then starts at the same left edge
    because none of them is indented at all.
    """
    _app, window = _shown_window(tmp_path, page=1, theme=theme)
    rows = _first_fields(window)
    assert len(rows) >= 4
    assert len({x for _title, x in rows}) == 1, rows
    _release(window)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_every_settings_field_is_the_same_height(tmp_path, theme) -> None:
    """A spin box asks for 3px more than a line edit and has to be capped.

    Styling the box without capping it left 端口 and 磁盘保留空间 standing taller
    than every other row on the page.  The cap is written from the theme's own
    control height, so every theme has to be checked, not just the shipped one.
    """
    _app, window = _shown_window(tmp_path, page=1, theme=theme)
    heights = {
        "host": window.host_input.height(),
        "port": window.port_input.height(),
        "reserve": window.reserve_input.height(),
        "bandwidth": window.bandwidth_input.height(),
        "theme": window.theme_input.height(),
    }
    assert len(set(heights.values())) == 1, heights
    assert heights["host"] == window.theme.metrics.control_height + 2
    _release(window)


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_keyboard_focus_costs_no_geometry(tmp_path, theme) -> None:
    """The accent ring is two pixels the layout must not pay for.

    A focus border that is not compensated in padding and min-height grows the
    widget, which pushes the form row taller and steps the text sideways - so
    tabbing through the page makes it twitch.  The compensation is derived from
    the theme's control height rather than written per theme, and this is the
    test that proves the derivation still holds after the numbers change.
    """
    app, window = _shown_window(tmp_path, page=1, theme=theme)
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
    _release(window)


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


def _sources() -> tuple[str, str]:
    """The theme table and the window, as text, for the dead-token check below."""
    import smsi_archive_sync.themes as themes_module
    import smsi_archive_sync.ui as ui_module
    from pathlib import Path

    return (
        Path(themes_module.__file__).read_text(encoding="utf-8"),
        Path(ui_module.__file__).read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_every_theme_declares_a_usable_table(theme) -> None:
    """A theme is data, and this is the check that the data is complete.

    No Qt here: it runs on the numbers.  The two things that can silently go
    wrong when a second theme is written by hand are a reading with no width
    declared for it - which raises only when that column is reached - and a
    colour that the theme sets but nothing paints, which leaves the light value
    showing through the dark theme.
    """
    resolved = THEMES[theme]
    assert normalize_theme_name(resolved.name) == resolved.name
    assert resolved.label and resolved.caption
    sheet = build_qss(resolved)
    assert "$" not in sheet
    metrics = resolved.metrics
    # Eight readings on the overview strip, so eight widths and a column count
    # that can lay them out without a hole.
    assert len(metrics.metric_min_widths) == 8
    assert 8 % max(1, metrics.summary_columns) == 0
    assert {index for index, _width in metrics.tree_columns} == set(range(1, 7))
    themes_source, ui_source = _sources()
    for token in vars(resolved.palette):
        used = f"${token}" in themes_source or f'"{token}"' in ui_source
        assert used, f"{token} is set by every theme and painted by nothing"


def _luminance(colour: str) -> float:
    """Perceived brightness, 0 (black) to 1 (white)."""
    value = QColor(colour)
    return (
        0.2126 * value.redF() + 0.7152 * value.greenF() + 0.0722 * value.blueF()
    )


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_dark_is_a_measurement_not_a_flag(theme) -> None:
    """``dark=True`` decides whether Qt's own dialogs are repainted.

    A theme that sets the flag without darkening its surfaces - or darkens them
    without setting it - produces the one failure this client cannot show the
    user its way out of: a white message box with white text in it, because a
    message box paints from the palette and not from our stylesheet.
    """
    resolved = THEMES[theme]
    surface = _luminance(resolved.palette.window)
    text = _luminance(resolved.palette.text)
    assert resolved.dark is (surface < text)
    # Readable either way round: roughly the WCAG AA ratio for body text.
    assert abs(surface - text) > 0.5
    if resolved.dark:
        assert surface < 0.1


def test_switching_to_a_dark_theme_repaints_qt_dialogs_and_gives_them_back(
    tmp_path,
) -> None:
    """The palette has to travel with the stylesheet, and travel back.

    Restoring "our idea of light" instead of the palette the platform style
    handed us at startup is how a client ends up with a permanently slightly
    wrong light theme after one visit to the dark one.
    """
    app, window = _shown_window(tmp_path, page=1)
    light = app.palette().color(QPalette.ColorRole.Window)
    window.set_theme("watch")
    app.processEvents()
    dark = app.palette().color(QPalette.ColorRole.Window)
    assert _luminance(dark.name()) < _luminance(light.name())
    assert _luminance(dark.name()) < 0.1
    window.set_theme("standard")
    app.processEvents()
    assert app.palette().color(QPalette.ColorRole.Window) == light
    _release(window)


def _strip_rows(window) -> dict[int, int]:
    """How many readings the overview strip put on each row."""
    grid = window._summary_grid
    rows: dict[int, int] = {}
    for cell in window._summary_cells:
        index = grid.indexOf(cell)
        assert index >= 0, "a reading was left out of the strip"
        row = grid.getItemPosition(index)[0]
        rows[row] = rows.get(row, 0) + 1
    return rows


def test_the_overview_strip_reflows_instead_of_being_rebuilt(tmp_path) -> None:
    """Eight readings in one ruled row, or four across two rows with no rules.

    Same eight widgets either way: a switch that deleted and rebuilt them would
    also throw away whatever the strip is currently showing, and this is the
    surface an unattended machine is left displaying.
    """
    app, window = _shown_window(tmp_path)
    identity = [id(cell) for cell in window._summary_cells]
    assert _strip_rows(window) == {0: 8}
    assert [d.isHidden() for d in window._summary_dividers] == [False] * 7
    assert window._summary_dividers[0].height() == window.theme.metrics.summary_divider_height

    window.set_theme("watch")
    app.processEvents()
    assert _strip_rows(window) == {0: 4, 1: 4}
    # A hairline separates neighbours in a row; between two rows it would be a
    # grid nobody asked for, so the board hides them rather than dropping them.
    assert all(divider.isHidden() for divider in window._summary_dividers)
    assert [id(cell) for cell in window._summary_cells] == identity

    window.set_theme("standard")
    app.processEvents()
    assert _strip_rows(window) == {0: 8}
    assert [d.isHidden() for d in window._summary_dividers] == [False] * 7
    assert [id(cell) for cell in window._summary_cells] == identity
    _release(window)


def test_switching_theme_keeps_the_work_in_progress(tmp_path) -> None:
    """Nothing is rebuilt, so nothing on screen is lost.

    The window this runs on has a day open in the tree and an edit in a settings
    box that has not been saved.  A switch that reconstructed the pages would
    collapse the first and discard the second.
    """
    app, window = _shown_window(tmp_path)
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
    window._tree_day_items[day.key].setExpanded(True)
    window.host_input.setText("192.168.2.241")
    app.processEvents()
    before = window.tree.rowHeight(window.tree.model().index(0, 0))

    window.set_theme("watch")
    app.processEvents()
    assert window._tree_day_items[day.key].isExpanded() is True
    assert (day.key, day.objects[0].relative_key) in window._tree_file_items
    assert window.host_input.text() == "192.168.2.241"
    assert window.pages.currentIndex() == 0
    # And the tree did get the message: taller rows are the point of the board.
    assert window.tree.rowHeight(window.tree.model().index(0, 0)) > before
    _release(window)


def test_the_tray_and_the_settings_page_agree_about_the_theme(tmp_path) -> None:
    """Two ways in, one answer, and 保存 must not undo either of them.

    The window is normally hidden behind the tray icon, so the submenu is the
    only theme control some operators ever see.  Because the choice is written
    the moment it is made, the settings form has to carry it forward - otherwise
    saving an unrelated setting a minute later silently reverts the theme.
    """
    app, window = _shown_window(tmp_path, page=1)
    # The offscreen platform reports no system tray, so _build_tray returns
    # before it gets this far; the submenu itself is what is under test.
    window._build_theme_menu(QMenu(window))
    window._theme_actions["watch"].trigger()
    app.processEvents()
    assert window.theme.name == "watch"
    assert window.theme_input.currentData() == "watch"
    assert window.theme_caption.text() == THEMES["watch"].caption
    checked = {name for name, action in window._theme_actions.items() if action.isChecked()}
    assert checked == {"watch"}
    assert window.config_store.load().theme == "watch"

    window.reserve_input.setValue(12)
    window.save_settings()
    saved = window.config_store.load()
    assert (saved.theme, saved.minimum_free_gib) == ("watch", 12)
    _release(window)


def test_an_unknown_stored_theme_never_blocks_startup(tmp_path) -> None:
    """A hand-edited or downgraded config must still open the window.

    ``ConfigStore.load`` turns any validation error into a refusal to start, so
    the theme is normalised on the way in rather than validated: the one setting
    whose only repair surface is the window itself cannot be the setting that
    stops the window from opening.
    """
    store = ConfigStore(tmp_path / "config")
    store.path.write_text(
        json.dumps({**store.load().to_dict(), "theme": "midnight"}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert store.load().theme == DEFAULT_THEME_NAME
    assert AppConfig(theme="").theme == DEFAULT_THEME_NAME




