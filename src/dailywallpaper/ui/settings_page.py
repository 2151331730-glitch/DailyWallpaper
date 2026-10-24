# src/dailywallpaper/ui/settings_page.py
"""The settings tab (spec 11.2)."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import time

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dailywallpaper.app_autostart import AutostartService, default_command
from dailywallpaper.config import save_config

logger = logging.getLogger(__name__)


def _valid_time(text: str) -> bool:
    try:
        time.fromisoformat(text.strip())
    except ValueError:
        return False
    return True


class SettingsPage(QWidget):
    """Edits the handful of config values a user actually changes."""

    def __init__(self, config, config_path, on_saved=None, parent=None, autostart=None):
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.on_saved = on_saved
        self.autostart = autostart

        self.daily_time_edit = QLineEdit(config.app.daily_time)
        self.latest_time_edit = QLineEdit(config.app.latest_change_time)
        self.city_edit = QLineEdit(config.weather.city)
        self.budget_edit = QLineEdit(str(config.budget.monthly_cny))
        # The two settings a new user in China cannot live without: a proxy
        # for Wallhaven and a working Zhipu key. Both were file-only before.
        self.proxy_edit = QLineEdit(str(config.sources.proxy or ""))
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7892（留空则直连）")
        self.proxy_test_button = QPushButton("测试代理")
        self.proxy_test_button.clicked.connect(self.test_proxy)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            "已保存的 key 不会显示；留空表示不修改"
            if self._has_api_key()
            else "粘贴智谱 API key（open.bigmodel.cn）"
        )
        self.api_key_test_button = QPushButton("测试 key")
        self.api_key_test_button.clicked.connect(self.test_api_key)
        self.wallhaven_check = QCheckBox("启用 Wallhaven")
        self.wallhaven_check.setChecked(config.sources.wallhaven_enabled)
        self.bing_check = QCheckBox("启用 Bing 每日壁纸")
        self.bing_check.setChecked(config.sources.bing_enabled)
        self.manual_counts_check = QCheckBox("手动更换计入每日一次")
        self.manual_counts_check.setChecked(
            config.app.manual_change_counts_toward_daily_limit
        )
        self.manual_override_check = QCheckBox("今日已换过后仍允许手动再换")
        self.manual_override_check.setChecked(
            config.app.allow_manual_override_after_daily_change
        )
        self.autostart_check = QCheckBox("开机自动启动")
        self.autostart_check.setChecked(config.app.start_with_windows)

        self.steam_button = QPushButton("检测 Steam 环境")
        self.steam_button.clicked.connect(self.check_steam)
        self.steam_status = QLabel("")

        form = QFormLayout()
        form.addRow("每日更换时间", self.daily_time_edit)
        form.addRow("最晚更换时间", self.latest_time_edit)
        form.addRow("天气城市", self.city_edit)
        form.addRow("月度预算 (¥)", self.budget_edit)
        form.addRow("代理（可选）", self.proxy_edit)
        form.addRow("", self.proxy_test_button)
        form.addRow("智谱 API key", self.api_key_edit)
        form.addRow("", self.api_key_test_button)
        form.addRow(self.wallhaven_check)
        form.addRow(self.bing_check)
        form.addRow(self.manual_counts_check)
        form.addRow(self.manual_override_check)
        form.addRow(self.autostart_check)

        self.save_button = QPushButton("保存")
        self.save_button.clicked.connect(self.save)
        self.status = QLabel("")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save_button)
        layout.addWidget(self.status)
        layout.addSpacing(12)
        layout.addWidget(self.steam_button)
        layout.addWidget(self.steam_status)
        layout.addStretch(1)

    def check_steam(self) -> None:
        """Find Wallpaper Engine and report whether Steam is reachable.

        The result is only written to the config when the user saves, so a
        detection tick can never surprise them with a changed setting.
        """
        from dailywallpaper.steam_env import (
            apply_to_config,
            check_sites,
            detect,
            is_wallpaper_engine_dir,
        )

        configured = str(self.config.wallpaper_engine.install_dir or "").strip()
        auto = getattr(self.config.wallpaper_engine, "auto_detect", True)
        if is_wallpaper_engine_dir(configured):
            install = configured
            prefix = (
                "已找到 Wallpaper Engine。"
                if auto
                else "已找到 Wallpaper Engine（手动配置路径，自动探测已关闭）。"
            )
        elif not auto:
            # The user opted out of detection; guessing for them would be a
            # surprise, so say what is missing instead.
            self.steam_status.setText(
                "自动探测已关闭，且 config.toml 里没有可用的 install_dir。"
                "填入路径，或把 auto_detect 改回 true。"
            )
            return
        else:
            install = detect().we_install_dir
            prefix = "已找到 Wallpaper Engine。"
        if install is None:
            self.steam_status.setText(
                "没找到 Wallpaper Engine。先在 Steam 商店购买并安装它"
                "（详见随附的《Steam 壁纸设置》）。"
            )
            return
        if auto:
            self.config = apply_to_config(self.config)
        blocked = [
            name
            for name, ok in check_sites(
                proxy=self.config.sources.proxy, timeout=2.0
            ).items()
            if not ok
        ]
        if blocked:
            self.steam_status.setText(
                prefix + f"打不开：{'、'.join(blocked)}。"
                "开 Watt Toolkit（Steam++）商店/社区加速或游戏加速器后重试。"
            )
        elif auto:
            self.steam_status.setText(prefix + "商店和社区都能打开。点“保存”写入配置。")
        else:
            self.steam_status.setText(prefix + "商店和社区都能打开。")

    def _has_api_key(self) -> bool:
        try:
            import keyring

            from dailywallpaper.analysis.vision import (
                KEYRING_ACCOUNT,
                KEYRING_SERVICE,
            )

            return bool(keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT))
        except Exception:
            return False

    def test_proxy(self) -> None:
        from dailywallpaper.net import proxy_alive

        proxy = self.proxy_edit.text().strip()
        if not proxy:
            self.status.setText("没有填写代理；将使用直连")
            return
        self.status.setText(
            "代理端口可以连接" if proxy_alive(proxy, timeout=1.5)
            else "连不上这个代理端口：检查客户端是否在运行、端口是否正确"
        )

    def test_api_key(self) -> None:
        """Send one tiny request: a wrong or expired key must say so now."""
        key = self.api_key_edit.text().strip()
        if not key:
            self.status.setText(
                "已保存过 key（输入新 key 可替换）；测试请先粘贴一个 key"
                if self._has_api_key()
                else "还没有 key：去 open.bigmodel.cn 申请后粘贴到这里"
            )
            return
        from dailywallpaper.recommend.intents import classify_note

        self.status.setText("正在测试 key…")
        try:
            result = classify_note("test", key)
        except Exception as exc:
            self.status.setText(f"key 测试失败：{exc}")
            return
        if result is None:
            self.status.setText("key 请求失败：请看日志，确认 key 有效、余额足够")
            return
        self.status.setText("key 可用，点“保存”写入")

    def collect(self):
        """Return a new Config, or None when a field is invalid."""
        if not _valid_time(self.daily_time_edit.text()):
            self.status.setText("每日更换时间格式不对（例：08:30）")
            return None
        if not _valid_time(self.latest_time_edit.text()):
            self.status.setText("最晚更换时间格式不对（例：22:00）")
            return None
        try:
            budget = float(self.budget_edit.text())
        except ValueError:
            self.status.setText("月度预算必须是数字")
            return None
        if budget < 0:
            self.status.setText("月度预算不能为负")
            return None

        return replace(
            self.config,
            app=replace(
                self.config.app,
                daily_time=self.daily_time_edit.text().strip(),
                latest_change_time=self.latest_time_edit.text().strip(),
                manual_change_counts_toward_daily_limit=self.manual_counts_check.isChecked(),
                allow_manual_override_after_daily_change=(
                    self.manual_override_check.isChecked()
                ),
                start_with_windows=self.autostart_check.isChecked(),
            ),
            sources=replace(
                self.config.sources,
                wallhaven_enabled=self.wallhaven_check.isChecked(),
                bing_enabled=self.bing_check.isChecked(),
                proxy=self.proxy_edit.text().strip(),
            ),
            weather=replace(self.config.weather, city=self.city_edit.text().strip()),
            budget=replace(self.config.budget, monthly_cny=budget),
        )

    def _apply_autostart(self, wanted: bool) -> None:
        try:
            service = self.autostart or AutostartService(
                command=default_command(self.config_path)
            )
            service.sync(wanted)
        except Exception as exc:  # a locked registry must not lose the config
            logger.warning(f"autostart toggle failed: {exc}")

    def _store_api_key(self) -> None:
        """Write a newly typed key to the credential manager."""
        key = self.api_key_edit.text().strip()
        if not key:
            return
        try:
            import keyring

            from dailywallpaper.analysis.vision import (
                KEYRING_ACCOUNT,
                KEYRING_SERVICE,
            )

            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)
            self.api_key_edit.clear()
            self.api_key_edit.setPlaceholderText("已保存的 key 不会显示；留空表示不修改")
        except Exception as exc:
            logger.warning(f"api key save failed: {exc}")
            self.status.setText("key 写入凭据管理器失败")

    def save(self) -> bool:
        updated = self.collect()
        if updated is None:
            return False
        try:
            save_config(self.config_path, updated)
        except Exception as exc:  # a write failure must not look like success
            logger.warning(f"config save failed: {exc}")
            self.status.setText("保存失败：无法写入 config.toml")
            return False
        self._store_api_key()
        self._apply_autostart(updated.app.start_with_windows)
        self.config = updated
        self.status.setText("已保存")
        if self.on_saved is not None:
            self.on_saved(updated)
        return True
