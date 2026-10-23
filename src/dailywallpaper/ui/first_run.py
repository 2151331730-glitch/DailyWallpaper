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
from dailywallpaper.steam_env import (
    check_sites,
    detect,
    is_wallpaper_engine_dir,
    workshop_dir_for,
)

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


def _setup_doc_hint() -> str:
    """The guide's real path, or nothing when this build does not ship it."""
    from dailywallpaper.resources import resource

    doc = resource("docs", "STEAM_SETUP.md")
    if not doc.is_file():
        return ""
    return f"详见 {doc}"


def _ready_report(install) -> list[str]:
    lines = [f"· 已找到 Wallpaper Engine：{install}"]
    workshop = workshop_dir_for(install)
    subscribed = 0
    if workshop is not None:
        try:
            if workshop.is_dir():
                subscribed = sum(1 for item in workshop.iterdir() if item.is_dir())
        except OSError:
            # A locked or unreadable Library folder must not crash first run.
            subscribed = 0
    if subscribed:
        lines.append(f"· 创意工坊已订阅 {subscribed} 项，会参与每天的推荐。")
    else:
        lines.append("· 创意工坊还没订阅：在 Steam 创意工坊搜 wallpaper，点“订阅”，")
        lines.append("  订阅的内容会自动下载，然后重启本程序。")
    return lines


def _steam_report(cfg) -> list[str]:
    """Report the state the app will actually run with, not a fresh guess."""
    configured = str(getattr(cfg.wallpaper_engine, "install_dir", "") or "").strip()
    if is_wallpaper_engine_dir(configured):
        return _ready_report(Path(configured))
    if not getattr(cfg.wallpaper_engine, "auto_detect", True):
        return [
            "· 自动探测已关闭，且没有配置可用的 Wallpaper Engine 路径。",
            "  在 config.toml 的 [wallpaper_engine] 填 install_dir，",
            "  或把 auto_detect 改回 true 让程序自己找。",
        ]
    env = detect()
    if env.we_install_dir is not None:
        return _ready_report(env.we_install_dir)
    if env.steam_root is None:
        lines = [
            "· 没检测到 Steam：现在只做静态壁纸，动态壁纸不可用。",
            "  想用 Steam 壁纸：装 Steam，在商店买 Wallpaper Engine（¥19），",
            "  装好后重启本程序会自动识别。",
        ]
        hint = _setup_doc_hint()
        if hint:
            lines.append(f"  {hint}")
        return lines
    if not env.we_owned:
        return [
            "· 找到了 Steam，但库里没有 Wallpaper Engine。",
            "  在 Steam 商店搜 Wallpaper Engine（appid 431960，¥19）购买安装，",
            "  再重启本程序即可自动识别。",
        ]
    return [
        "· Steam 里买了 Wallpaper Engine，但程序没找到它的安装目录。",
        "  先在 Steam 里启动一次 Wallpaper Engine，再重启本程序。",
    ]


def _network_report(cfg) -> list[str]:
    status = check_sites(proxy=cfg.sources.proxy, timeout=2.0)
    blocked = [name for name, ok in status.items() if not ok]
    if blocked:
        return [
            "· 打不开：" + "、".join(blocked),
            "  国内直连打不开 Steam 商店/社区是正常的。任选一种工具后重试：",
            "  · Watt Toolkit（Steam++）：开启“商店/社区加速”，免费、常驻即可；",
            "  · 游戏加速器（UU/雷神等）：选 Steam 商店加速。",
            "  开好之后可点设置页的“检测 Steam 环境”。",
            "  （如果浏览器现在就能打开，这条可以忽略。）",
        ]
    return ["· Steam 商店和社区都能打开，可以直接购买和订阅。"]


def _environment_report(cfg) -> str:
    lines = _steam_report(cfg)
    lines.extend(_network_report(cfg))
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
