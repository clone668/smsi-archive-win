"""Outbound alerting for the Windows archive client.

This client holds the last copy in the chain and is also the copy nobody
watches: the window normally sits minimised in the tray, so a failure drawn on
screen is a failure nobody sees.  Three triggers are covered here:

* recorded events at or above a configured level, forwarded once each;
* *no successful scan* while automatic sync is on - a client whose loop has
  stalled records no events at all, which event-driven alerting cannot catch;
* *upstream silence* - the Ubuntu verified copy has stopped publishing new
  archive days, which is a fault further up the chain rather than here.

The poll runs on a plain daemon thread rather than a Qt timer, so a busy or
frozen window cannot mute alerting.  Nothing here touches Qt, the archive tree,
or any file being synced: an alert that cannot be delivered is retried on the
next poll and never fails a download or a verification.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .config import AppConfig, ConfigStore
from .state import StateStore


LOGGER = logging.getLogger(__name__)
ALERT_STATE_KEY = "alert_notifier"
ARCHIVE_WATCH_KEY = "archive_watch"
LEVEL_RANK = {"info": 10, "warning": 20, "error": 30}
LEVEL_MARKS = {"info": "·", "warning": "!", "error": "X"}
LEVEL_LABELS = {"info": "信息", "warning": "警告", "error": "错误"}
MAX_EVENTS_PER_MESSAGE = 8
MAX_MESSAGE_CHARS = 3500  # Telegram rejects anything past 4096.
POLL_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 10
TELEGRAM_ENDPOINT = "https://api.telegram.org/bot{token}/sendMessage"

Sender = Callable[[str, str, str], None]

def level_rank(level: str) -> int:
    return LEVEL_RANK.get(str(level or "").strip().lower(), LEVEL_RANK["info"])


def forwarded_levels(minimum: str) -> list[str]:
    """Event levels at or above the configured minimum."""
    threshold = level_rank(minimum)
    return [name for name, rank in LEVEL_RANK.items() if rank >= threshold]


def _scrub(text: str, secret: str) -> str:
    """Keep the bot token out of anything that can be logged or displayed."""
    cleaned = str(text or "")
    if secret and len(secret) > 8:
        cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    """Telegram explains refusals in the body; that text is what the user needs."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        payload = {}
    description = ""
    if isinstance(payload, Mapping):
        description = str(payload.get("description") or "")
    return description or f"HTTP {exc.code}"


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Deliver one message, or raise RuntimeError with a token-free reason."""
    url = TELEGRAM_ENDPOINT.format(token=token)
    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            payload = json.loads(reply.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_scrub(_describe_http_error(exc), token)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(_scrub(str(exc) or exc.__class__.__name__, token)) from exc
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        detail = ""
        if isinstance(payload, Mapping):
            detail = str(payload.get("description") or "")
        raise RuntimeError(_scrub(detail or "Telegram 拒绝了这条消息", token))

def parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _short_time(value: str) -> str:
    moment = parse_utc(value)
    return moment.strftime("%m-%d %H:%M") if moment else "--"


def _describe_hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f}" if hours < 10 else str(int(hours))


def archive_watch_record(scanned_at: str, archive_dates: Sequence[str]) -> dict[str, Any]:
    """The two facts alerting needs from a scan, kept small on purpose.

    Reading the whole cached catalogue on every poll would parse megabytes to
    answer one question, so a scan leaves just its own timestamp and the newest
    archive day it saw.
    """
    newest = ""
    for value in archive_dates:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            date.fromisoformat(text)
        except ValueError:
            continue
        if text > newest:
            newest = text
    return {"scanned_at": str(scanned_at or ""), "newest_archive_date": newest}

class AlertNotifier:
    """Forwards recorded events, a stalled scan loop and upstream silence.

    State lives in the local database so that a restart neither replays old
    alerts nor forgets an alarm that is still active.  ``sender`` and ``now``
    are injectable so tests never touch the network or the wall clock.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        state_store: StateStore,
        *,
        sender: Sender | None = None,
        hostname: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config_store = config_store
        self.state_store = state_store
        self._sender = sender or send_telegram
        self._hostname = hostname or socket.gethostname()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _stamp(self) -> str:
        """Every timestamp this class writes or compares comes from one clock."""
        return self._now().isoformat().replace("+00:00", "Z")

    def _load_state(self) -> dict[str, Any]:
        value = self.state_store.get_runtime(ALERT_STATE_KEY, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_store.set_runtime(ALERT_STATE_KEY, dict(state))

    def _watch(self) -> dict[str, Any]:
        value = self.state_store.get_runtime(ARCHIVE_WATCH_KEY, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def record_scan(self, scanned_at: str, archive_dates: Sequence[str]) -> None:
        """Called after a successful scan; both time triggers measure this."""
        self.state_store.set_runtime(
            ARCHIVE_WATCH_KEY, archive_watch_record(scanned_at, archive_dates)
        )

    def state(self) -> dict[str, Any]:
        """Delivery health for the settings page: alerting itself can fail."""
        state = self._load_state()
        return {
            "watching_since": str(state.get("watch_started_at") or ""),
            "last_sent_at": str(state.get("last_sent_at") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_error_at": str(state.get("last_error_at") or ""),
            "scan_alerted": bool(state.get("scan_alerted")),
            "upstream_alerted": bool(state.get("upstream_alerted")),
            "pending_events": bool(state.get("pending_events")),
        }

    def _compose(self, headline: str, lines: Sequence[str]) -> str:
        # The header names this client: both clients usually report to the same
        # chat, so a message that does not say who is speaking is useless.
        head = f"【SMSI 归档同步 Windows】{self._hostname}"
        text = "\n".join([head, headline, ""] + list(lines))
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
        return text[: MAX_MESSAGE_CHARS - 9] + "\n…（已截断）"

    def _event_lines(self, events: Sequence[Mapping[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in events:
            mark = LEVEL_MARKS.get(str(item.get("level") or ""), "·")
            when = _short_time(str(item.get("created_at") or ""))
            lines.append(f"{mark} {when} {item.get('message') or ''}".rstrip())
            detail = str(item.get("detail") or "").strip().replace("\n", " ")
            if detail:
                lines.append(f"    {detail[:200]}")
        return lines

    def _scan_silence_lines(
        self, config: AppConfig, watch: Mapping[str, Any], state: Mapping[str, Any]
    ) -> tuple[list[str], bool | None]:
        """Report a scan loop that has stopped producing successful scans.

        Only meaningful while automatic sync is on: with it off the user drives
        every scan by hand, so quiet is a choice rather than a fault.  Turning
        automatic sync off clears a latched alarm silently instead of claiming a
        recovery that did not happen.
        """
        alerted = bool(state.get("scan_alerted"))
        if not config.auto_sync_enabled:
            return [], False if alerted else None
        reference = parse_utc(str(watch.get("scanned_at") or "")) or parse_utc(
            str(state.get("watch_started_at") or "")
        )
        if reference is None:
            return [], None
        idle = self._now() - reference
        if idle >= timedelta(hours=max(1, int(config.alert_stale_hours))):
            if alerted:
                return [], None
            return [
                f"! 已经 {_describe_hours(idle)} 小时没有成功扫描 Ubuntu 归档",
                f"    上次成功：{_short_time(reference.isoformat())}",
                "    自动同步已开启但没有完成扫描，请检查客户端与网络",
            ], True
        if alerted:
            return [f"· 扫描已恢复，上次成功 {_short_time(reference.isoformat())}"], False
        return [], None

    def _upstream_lines(
        self, config: AppConfig, watch: Mapping[str, Any], state: Mapping[str, Any]
    ) -> tuple[list[str], bool | None]:
        """Report that the Ubuntu verified copy stopped publishing new days.

        This is only trustworthy while our own view is fresh: a stale catalogue
        would let this client blame the collector for its own blindness, so a
        scan older than the silence threshold is left to that alarm instead.
        """
        newest = str(watch.get("newest_archive_date") or "")
        scanned = parse_utc(str(watch.get("scanned_at") or ""))
        alerted = bool(state.get("upstream_alerted"))
        if not newest or scanned is None:
            return [], None
        if self._now() - scanned >= timedelta(hours=max(1, int(config.alert_stale_hours))):
            return [], None
        try:
            lag = (self._now().date() - date.fromisoformat(newest)).days
        except ValueError:
            return [], None
        if lag > max(2, int(config.alert_upstream_lag_days)):
            if alerted:
                return [], None
            return [
                f"! Ubuntu 已验证副本没有新的归档日，最新为 {newest}",
                f"    已滞后 {lag} 天，正常情况下每天新增一天",
                "    请检查采集服务器与 Ubuntu 归档备份客户端",
            ], True
        if alerted:
            return [f"· 数据源已恢复，最新归档日 {newest}"], False
        return [], None

    def poll(self) -> dict[str, Any]:
        """One alerting pass.  Never raises: alerting must not break the app."""
        try:
            config = self.config_store.load()
        except Exception as exc:  # noqa: BLE001 - a bad config must not stop work
            return {"ok": False, "sent": 0, "error": str(exc)[:200]}
        if not config.alert_enabled:
            return {"ok": True, "sent": 0, "skipped": "disabled"}
        state = self._load_state()
        try:
            token = config.alert_token().strip()
        except RuntimeError as exc:
            # A token the local account can no longer decrypt is a real fault,
            # and the only place left to report it is the settings page.
            self._record_failure(state, str(exc)[:200])
            return {"ok": False, "sent": 0, "error": str(exc)[:200]}
        chat_id = config.alert_chat_id.strip()
        if not token or not chat_id:
            return {"ok": True, "sent": 0, "skipped": "unconfigured"}
        if not state.get("watch_started_at"):
            # Enabling alerts must not replay the whole event history, so the
            # first pass only marks where "new" begins.
            state["watch_started_at"] = self._stamp()
            state["last_event_id"] = self.state_store.max_event_id()
            self._save_state(state)
            return {"ok": True, "sent": 0, "skipped": "initialised"}
        return self._deliver(config, token, chat_id, state)

    def _record_failure(self, state: dict[str, Any], reason: str) -> None:
        """One place for "the alert channel itself is broken"."""
        if reason != str(state.get("last_error") or ""):
            # Reporting a broken notification channel through that same channel
            # cannot work, so this is recorded locally - and only once per
            # reason, otherwise a permanent fault fills the event list.
            self.state_store.event(
                "info", "告警通知发送失败", f"{reason}（本次告警会自动重试）"
            )
        state["last_error"] = reason
        state["last_error_at"] = self._stamp()
        state["pending_events"] = True
        self._save_state(state)

    def _deliver(
        self, config: AppConfig, token: str, chat_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        watermark = int(state.get("last_event_id") or 0)
        levels = forwarded_levels(config.alert_min_level)
        events = self.state_store.events_since(
            watermark, levels, limit=MAX_EVENTS_PER_MESSAGE
        )
        watch = self._watch()
        scan_lines, scan_flag = self._scan_silence_lines(config, watch, state)
        upstream_lines, upstream_flag = self._upstream_lines(config, watch, state)
        lines = scan_lines + upstream_lines + self._event_lines(events)
        if not lines:
            # Keep the cursor moving even when nothing qualified, otherwise the
            # query rescans an ever-growing tail of the event list.
            newest = self.state_store.max_event_id()
            changed = newest != watermark or state.get("pending_events")
            for key, flag in (("scan_alerted", scan_flag), ("upstream_alerted", upstream_flag)):
                if flag is not None and bool(state.get(key)) != flag:
                    state[key] = flag
                    changed = True
            if changed:
                state["last_event_id"] = newest
                state["pending_events"] = False
                self._save_state(state)
            return {"ok": True, "sent": 0, "skipped": "nothing_to_report"}
        counts: dict[str, int] = {}
        for item in events:
            level = str(item.get("level") or "info")
            counts[level] = counts.get(level, 0) + 1
        parts = [
            f"{count} 条{LEVEL_LABELS.get(level, level)}"
            for level, count in counts.items()
        ]
        if scan_lines:
            parts.append("扫描中断" if scan_flag else "扫描已恢复")
        if upstream_lines:
            parts.append("数据源停更" if upstream_flag else "数据源已恢复")
        headline = "、".join(parts) or "状态更新"

        try:
            self._sender(token, chat_id, self._compose(headline, lines))
        except RuntimeError as exc:
            self._record_failure(state, str(exc)[:200])
            return {"ok": False, "sent": 0, "error": str(exc)[:200]}
        if events:
            state["last_event_id"] = max(int(item.get("id") or 0) for item in events)
        if scan_flag is not None:
            state["scan_alerted"] = scan_flag
        if upstream_flag is not None:
            state["upstream_alerted"] = upstream_flag
        state["pending_events"] = bool(
            self.state_store.events_since(
                int(state.get("last_event_id") or 0), levels, limit=1
            )
        )
        state["last_sent_at"] = self._stamp()
        state["last_error"] = ""
        state["last_error_at"] = ""
        self._save_state(state)
        return {"ok": True, "sent": len(lines), "events": len(events)}

    def send_test(self) -> dict[str, Any]:
        """Prove the channel works now, without touching the alert cursor."""
        config = self.config_store.load()
        token = config.alert_token().strip()
        chat_id = config.alert_chat_id.strip()
        if not token or not chat_id:
            raise RuntimeError("请先填写 Telegram 机器人令牌与会话 ID")
        text = self._compose(
            "测试消息",
            [
                "· 告警通道连通性测试",
                f"    发送时间 {_short_time(self._stamp())} UTC",
            ],
        )
        self._sender(token, chat_id, text)
        state = self._load_state()
        state["last_test_at"] = self._stamp()
        state["last_error"] = ""
        state["last_error_at"] = ""
        if not state.get("watch_started_at"):
            state["watch_started_at"] = self._stamp()
            state["last_event_id"] = self.state_store.max_event_id()
        self._save_state(state)
        return {"ok": True}

class AlertWorker:
    """Polls the notifier on a daemon thread, off the Qt event loop.

    A Qt timer would stop firing exactly when it matters most - while the window
    is busy or wedged - so alerting gets its own thread.  It touches only the
    local database, the config file and the network, never a widget.
    """

    def __init__(self, notifier: AlertNotifier, *, interval: int = POLL_SECONDS) -> None:
        self.notifier = notifier
        self.interval = max(15, int(interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="smsi-alert-worker", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        """Apply a settings change now rather than at the next interval."""
        self._wake.set()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.notifier.poll()
            except Exception:  # noqa: BLE001 - the client must keep syncing
                LOGGER.exception("Alert poll failed")
            self._wake.wait(self.interval)
            self._wake.clear()
