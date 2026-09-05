from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from smsi_archive_sync.config import ConfigStore
from smsi_archive_sync.icons import application_icon
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.style import APP_STYLE
from smsi_archive_sync.ui import MainWindow


def configure_logging(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        root / "client.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    config_store = ConfigStore()
    configure_logging(config_store.root / "logs")
    logger = logging.getLogger(__name__)
    app = QApplication(sys.argv)
    app.setApplicationName("SMSIArchiveSync")
    app.setApplicationDisplayName("SMSI 归档同步")
    app.setOrganizationName("SMSI")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    lock = QLockFile(str(config_store.root / "client.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None, "SMSI 归档同步", "客户端已经在运行。")
        return 2
    window = MainWindow(config_store, StateStore(config_store.root), application_icon())
    window.show()
    logger.info("SMSI archive sync client started")
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
