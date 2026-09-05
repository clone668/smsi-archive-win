from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smsi_archive_sync.alerts import (
    MAX_EVENTS_PER_MESSAGE,
    AlertNotifier,
    _scrub,
    archive_watch_record,
)
from smsi_archive_sync.config import ConfigStore, validate_alert_token
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.storage import GIB


TOKEN = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
CHAT_ID = "-1001234567890"
START = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class FakeSender:
    """Records what would have been delivered, and can refuse on demand."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failure = ""

    def __call__(self, token: str, chat_id: str, text: str) -> None:
        assert token == TOKEN
        assert chat_id == CHAT_ID
        if self.failure:
            raise RuntimeError(self.failure)
        self.messages.append(text)


def _build(
    tmp_path: Path,
    *,
    enabled: bool = True,
    minimum: str = "warning",
    stale_hours: int = 6,
    lag_days: int = 3,
    auto_sync: bool = True,
):
    store = ConfigStore(tmp_path / "config")
    config = store.load()
    config.local_root = str(tmp_path / "archive")
    config.auto_sync_enabled = auto_sync
    config.alert_enabled = enabled
    config.set_alert_token(TOKEN)
    config.alert_chat_id = CHAT_ID
    config.alert_min_level = minimum
    config.alert_stale_hours = stale_hours
    config.alert_upstream_lag_days = lag_days
    store.save(config)
    state = StateStore(tmp_path / "state")
    clock = {"now": START}
    sender = FakeSender()
    notifier = AlertNotifier(
        store,
        state,
        sender=sender,
        hostname="win-archive",
        now=lambda: clock["now"],
    )
    return store, state, notifier, sender, clock


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def test_enabling_alerts_does_not_replay_the_event_history(tmp_path: Path) -> None:
    """Switching alerting on must not dump the whole event list into the chat."""
    _store, state, notifier, sender, _clock = _build(tmp_path)
    for index in range(5):
        state.event("error", f"历史错误 {index}")

    assert notifier.poll()["skipped"] == "initialised"
    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert sender.messages == []

    state.event("error", "下载失败", detail="连接被拒绝")
    result = notifier.poll()

    assert result["sent"]
    assert len(sender.messages) == 1
    assert "下载失败" in sender.messages[0]
    assert "历史错误" not in sender.messages[0]
    # Both clients report to the same chat, so the message must name this one.
    assert "win-archive" in sender.messages[0]
    assert "Windows" in sender.messages[0]


def test_each_event_is_forwarded_once_and_info_is_filtered(tmp_path: Path) -> None:
    _store, state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    state.event("info", "例行信息")
    state.event("warning", "同步已取消")
    state.event("error", "校验失败", detail="sha256 不一致")

    assert notifier.poll()["events"] == 2
    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert "同步已取消" in message
    assert "校验失败" in message
    assert "例行信息" not in message


def test_a_failed_delivery_is_retried_and_not_lost(tmp_path: Path) -> None:
    """An unreachable chat must not silently swallow the only warning."""
    _store, state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    state.event("error", "校验失败", detail="sha256 不一致")
    sender.failure = "Bad Request: chat not found"

    assert notifier.poll()["ok"] is False
    assert notifier.state()["last_error"] == "Bad Request: chat not found"
    assert notifier.state()["pending_events"] is True

    sender.failure = ""
    assert notifier.poll()["events"] == 1
    assert "校验失败" in sender.messages[0]
    assert notifier.state()["last_error"] == ""


def test_a_stalled_scan_loop_is_alerted_once_and_recovery_once(tmp_path: Path) -> None:
    """A client whose loop stalled records no events, so time is the signal."""
    _store, _state, notifier, sender, clock = _build(tmp_path, stale_hours=6)
    notifier.record_scan(_stamp(clock["now"]), ["2026-08-09"])
    notifier.poll()

    clock["now"] += timedelta(hours=7)
    assert notifier.poll()["sent"]
    assert "没有成功扫描" in sender.messages[0]
    assert notifier.state()["scan_alerted"] is True

    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1

    notifier.record_scan(_stamp(clock["now"]), ["2026-08-09"])
    assert notifier.poll()["sent"]
    assert "扫描已恢复" in sender.messages[1]
    assert notifier.state()["scan_alerted"] is False
    assert notifier.poll()["skipped"] == "nothing_to_report"


def test_turning_automatic_sync_off_clears_the_alarm_without_claiming_recovery(
    tmp_path: Path,
) -> None:
    """With automatic sync off the user drives every scan, so quiet is a choice."""
    store, _state, notifier, sender, clock = _build(tmp_path, stale_hours=6)
    notifier.record_scan(_stamp(clock["now"]), ["2026-08-09"])
    notifier.poll()
    clock["now"] += timedelta(hours=7)
    assert notifier.poll()["sent"]

    config = store.load()
    config.auto_sync_enabled = False
    store.save(config)

    assert notifier.poll()["skipped"] == "nothing_to_report"
    # The latch is released so a later stall alerts again, but no message
    # claimed a recovery that never happened.
    assert notifier.state()["scan_alerted"] is False
    assert len(sender.messages) == 1


def test_a_fresh_install_is_not_accused_of_a_stalled_scan(tmp_path: Path) -> None:
    """With no scan on record, silence is measured from when alerting began."""
    _store, _state, notifier, sender, clock = _build(tmp_path, stale_hours=6)
    notifier.poll()

    clock["now"] += timedelta(hours=3)
    assert notifier.poll()["skipped"] == "nothing_to_report"

    clock["now"] += timedelta(hours=4)
    assert notifier.poll()["sent"]
    assert "没有成功扫描" in sender.messages[0]


def test_upstream_silence_is_alerted_once_and_recovery_once(tmp_path: Path) -> None:
    """The Ubuntu copy publishing no new day is a fault further up the chain."""
    _store, _state, notifier, sender, clock = _build(tmp_path, lag_days=3)
    notifier.record_scan(_stamp(clock["now"]), ["2026-08-05", "2026-08-04"])
    notifier.poll()

    assert notifier.poll()["sent"]
    assert "没有新的归档日" in sender.messages[0]
    assert "2026-08-05" in sender.messages[0]
    assert "已滞后 5 天" in sender.messages[0]
    assert notifier.state()["upstream_alerted"] is True

    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1

    notifier.record_scan(_stamp(clock["now"]), ["2026-08-09"])
    assert notifier.poll()["sent"]
    assert "数据源已恢复" in sender.messages[1]
    assert notifier.state()["upstream_alerted"] is False


def test_a_stale_catalogue_never_blames_the_data_source(tmp_path: Path) -> None:
    """Our own blindness must not be reported as somebody else's fault."""
    _store, _state, notifier, sender, clock = _build(
        tmp_path, lag_days=3, auto_sync=False
    )
    notifier.record_scan(_stamp(clock["now"]), ["2026-08-05"])
    notifier.poll()

    clock["now"] += timedelta(hours=7)
    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert sender.messages == []
    assert notifier.state()["upstream_alerted"] is False

    # The same lag, now seen by a fresh scan, is a real upstream fault.
    notifier.record_scan(_stamp(clock["now"]), ["2026-08-05"])
    assert notifier.poll()["sent"]
    assert "没有新的归档日" in sender.messages[0]


def test_a_broken_channel_is_recorded_locally_once_per_reason(tmp_path: Path) -> None:
    """Reporting a dead channel through that same channel cannot work."""
    _store, state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    state.event("error", "校验失败")
    sender.failure = "Unauthorized"

    for _ in range(3):
        assert notifier.poll()["ok"] is False

    notes = [row for row in state.recent_events(50) if row["message"] == "告警通知发送失败"]
    assert len(notes) == 1
    # Recorded at info level so it can never be forwarded as an alert itself.
    assert notes[0]["level"] == "info"


def test_a_repeated_fault_is_reported_once_with_the_count_of_its_repeats(
    tmp_path: Path,
) -> None:
    """Automatic sync retries every 15 minutes; one fault is still one fault."""
    _store, state, notifier, sender, clock = _build(tmp_path)
    notifier.poll()
    state.event("error", "操作失败", detail="连接超时")
    assert notifier.poll()["events"] == 1

    for attempt in range(3):
        clock["now"] += timedelta(minutes=15)
        state.event("error", "操作失败", detail=f"第 {attempt} 次重试仍然超时")
        result = notifier.poll()
        assert result["skipped"] == "repeats_suppressed"
        assert result["suppressed"] == 1
    assert len(sender.messages) == 1

    # A different fault is news and is never held back by the suppressed one.
    state.event("error", "磁盘空间不足", detail="可用 1 GiB")
    assert notifier.poll()["events"] == 1
    assert "磁盘空间不足" in sender.messages[1]
    assert "操作失败" not in sender.messages[1]

    clock["now"] += timedelta(minutes=30)
    state.event("error", "操作失败", detail="仍然连接超时")
    assert notifier.poll()["events"] == 1
    assert "操作失败（期间重复 3 次）" in sender.messages[2]


def test_suppressed_repeats_never_hide_a_fault_further_down_the_list(
    tmp_path: Path,
) -> None:
    """The cursor may pass repeats, never events this pass has not read."""
    _store, state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    state.event("error", "操作失败")
    assert notifier.poll()["events"] == 1

    for _ in range(MAX_EVENTS_PER_MESSAGE):
        state.event("error", "操作失败")
    state.event("error", "校验失败", detail="sha256 不一致")

    assert notifier.poll()["skipped"] == "repeats_suppressed"
    assert notifier.state()["pending_events"] is True
    assert notifier.poll()["events"] == 1
    assert "校验失败" in sender.messages[1]


def test_disabled_alerting_sends_nothing(tmp_path: Path) -> None:
    _store, state, notifier, sender, _clock = _build(tmp_path, enabled=False)
    state.event("error", "下载失败")

    assert notifier.poll()["skipped"] == "disabled"
    assert sender.messages == []


def test_the_token_is_encrypted_at_rest_and_survives_an_unrelated_save(
    tmp_path: Path,
) -> None:
    store, _state, _notifier, _sender, _clock = _build(tmp_path)

    raw = (tmp_path / "config" / "config.json").read_text(encoding="utf-8")
    assert TOKEN not in raw
    assert "dpapi:v1:" in raw
    assert store.load().alert_token() == TOKEN

    config = store.load()
    config.alert_stale_hours = 24
    store.save(config)
    assert store.load().alert_token() == TOKEN


def test_a_mistyped_token_is_refused_before_it_is_stored() -> None:
    assert validate_alert_token(f"  {TOKEN} ") == TOKEN
    assert validate_alert_token("") == ""
    for bad in ("not-a-token", "123456789:short", TOKEN[:20], "123456789 AAE" + "a" * 30):
        with pytest.raises(ValueError, match="令牌格式无效"):
            validate_alert_token(bad)


def test_alert_settings_are_validated_before_they_are_saved(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config")
    config = store.load()

    config.alert_enabled = True
    with pytest.raises(ValueError, match="必须填写机器人令牌与会话 ID"):
        store.save(config)

    config.alert_enabled = False
    config.alert_chat_id = "群聊"
    with pytest.raises(ValueError, match="会话 ID 无效"):
        store.save(config)

    config.alert_chat_id = CHAT_ID
    config.alert_upstream_lag_days = 1
    with pytest.raises(ValueError, match="2 至 14 天"):
        store.save(config)


def test_a_scan_leaves_only_the_two_facts_alerting_needs() -> None:
    record = archive_watch_record(
        "2026-08-10T12:00:00Z", ["2026-08-04", "2026-08-09", "", "坏数据"]
    )
    assert record == {
        "scanned_at": "2026-08-10T12:00:00Z",
        "newest_archive_date": "2026-08-09",
    }
    assert archive_watch_record("", [])["newest_archive_date"] == ""


def test_a_damaged_token_is_reported_instead_of_crashing_the_thread(
    tmp_path: Path,
) -> None:
    """A blob the local account can no longer decrypt is a real fault."""
    store, _state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    config = store.load()
    config.alert_token_blob = "dpapi:v1:not-base64!!"
    store.save(config)

    result = notifier.poll()

    assert result["ok"] is False
    assert notifier.state()["last_error"]
    assert sender.messages == []


def _fake_disk(monkeypatch) -> dict:
    """A volume reading the test controls, not the one the tests run on.

    Whether alerting fires must not depend on how full the developer's disk
    happens to be, so the reading is substituted and mutated in place.
    """
    reading = {
        "ok": True,
        "error": "",
        "free_bytes": 400 * GIB,
        "total_bytes": 900 * GIB,
        "reserve_bytes": 10 * GIB,
        "low": False,
    }
    monkeypatch.setattr(
        "smsi_archive_sync.alerts.disk_status", lambda *_args, **_kwargs: reading
    )
    return reading


def test_a_filling_archive_disk_is_alerted_before_it_stops_the_sync(
    tmp_path: Path, monkeypatch
) -> None:
    """The one fault that gives warning before it stops the archive.

    Below the reserve the client refuses to sync while the volume still has
    room, so the alarm has to arrive at that point - not after a day fails.
    """
    reading = _fake_disk(monkeypatch)
    _store, _state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()
    assert notifier.poll()["skipped"] == "nothing_to_report"

    reading.update({"free_bytes": 3 * GIB, "low": True})
    assert notifier.poll()["sent"]
    assert "可用空间不足" in sender.messages[0]
    assert "3.0 GiB" in sender.messages[0]
    assert notifier.state()["disk_alerted"] is True

    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1

    reading.update({"free_bytes": 120 * GIB, "low": False})
    assert notifier.poll()["sent"]
    assert "已恢复" in sender.messages[1]
    assert notifier.state()["disk_alerted"] is False
    assert notifier.poll()["skipped"] == "nothing_to_report"


def test_an_unreadable_archive_directory_is_not_reported_as_plenty_of_space(
    tmp_path: Path, monkeypatch
) -> None:
    """A disconnected archive disk is worse than a full one, and says so."""
    reading = _fake_disk(monkeypatch)
    _store, _state, notifier, sender, _clock = _build(tmp_path)
    notifier.poll()

    reading.update({"ok": False, "error": "[WinError 21] 设备未就绪", "free_bytes": 0})
    assert notifier.poll()["sent"]
    assert "无法访问" in sender.messages[0]
    assert "设备未就绪" in sender.messages[0]
    assert notifier.state()["disk_alerted"] is True

    assert notifier.poll()["skipped"] == "nothing_to_report"
    assert len(sender.messages) == 1



