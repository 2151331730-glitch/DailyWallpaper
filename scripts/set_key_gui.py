"""Store the Zhipu API key in Windows Credential Manager via a GUI dialog.

Use this when the terminal prompt cannot read input (for example, inside an
agent harness without a TTY). The key is never printed or written to disk.
"""

from __future__ import annotations

import sys

import keyring
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

SERVICE = "DailyWallpaper"
ACCOUNT = "zhipu"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("DailyWallpaper")
    key, ok = QInputDialog.getText(
        None,
        "DailyWallpaper",
        "Zhipu API key (stored in Windows Credential Manager):",
        QLineEdit.EchoMode.Password,
    )
    if not ok:
        return 1
    key = key.strip()
    if not key:
        QMessageBox.warning(None, "DailyWallpaper", "Empty key; nothing stored.")
        return 1
    keyring.set_password(SERVICE, ACCOUNT, key)
    QMessageBox.information(
        None,
        "DailyWallpaper",
        "Key stored in Windows Credential Manager.",
    )
    print("stored in Windows Credential Manager")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
