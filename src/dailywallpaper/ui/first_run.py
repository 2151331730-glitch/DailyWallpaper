# src/dailywallpaper/ui/first_run.py
"""First-launch setup: the API key, then a short environment report.

An installed copy has no key yet, and without one the pipeline refuses to
build -- the app would close on launch with a traceback nobody sees. Ask
first, then tell the user what else is missing (WE, proxy) once.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from dailywallpaper.analysis.vision import KEYRING_ACCOUNT, KEYRING_SERVICE
from dailywallpaper.config import install_prefs, user_data_dir

logger = logging.getLogger(__name__)


def _mark_done() -> None:
    prefs = install_prefs()
    prefs["first_run_done"] = True
    path = user_data_dir() / "install_prefs.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # a read-only profile must not block the app
        logger.warning(f"cannot record first run: {exc}")


def _environment_report(cfg) -> str:
    lines = []
    install = str(getattr(cfg.wallpaper_engine, "install_dir", "") or "").strip()
    if not install or not Path(install).is_dir():
        lines.append("· 没找到 Wallpaper Engine：只做静态壁纸，动态壁纸不可用")
    proxy = str(getattr(cfg.sources, "proxy", "") or "").strip()
    if proxy:
        from dailywallpaper.net import proxy_alive

        if not proxy_alive(proxy):
            lines.append(
                f"· 代理 {proxy} 没有监听：Wallhaven 不可用，只剩 Bing（一天一张）"
            )
    else:
        lines.append(
            "· 没有配置代理：Wallhaven 在国内需要代理，不开就只剩 Bing"
        )
    if not lines:
        lines.append("· Wallpaper Engine 和代理看起来都正常。")
    return "\n".join(lines)


def ensure_first_run(cfg) -> bool:
    """Return False when the user cancels the key prompt."""
    import keyring

    if not keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT):
        key, ok = QInputDialog.getText(
            None,
            "每日壁纸 · 首次设置",
            "需要一个智谱 API key 才能给壁纸打分。\n\n"
            "免费申请：open.bigmodel.cn → API Keys\n"
            "（key 只保存在 Windows 凭据管理器，不写入任何文件）",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not key.strip():
            return False
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key.strip())

    if not install_prefs().get("first_run_done"):
        QMessageBox.information(
            None,
            "每日壁纸 · 环境检查",
            "设置完成，可以开始了。\n\n" + _environment_report(cfg),
        )
        _mark_done()
    return True
