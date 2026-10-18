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
        self.wallhaven_check = QCheckBox("启用 Wallhaven")
        self.wallhaven_check.setChecked(config.sources.wallhaven_enabled)
        self.bing_check = QCheckBox("启用 Bing 每日壁纸")
        self.bing_check.setChecked(config.sources.bing_enabled)
        self.manual_counts_check = QCheckBox("手动更换计入每日一次")
        self.manual_counts_check.setChecked(
            config.app.manual_change_counts_toward_daily_limit
        )
        self.autostart_check = QCheckBox("开机自动启动")
        self.autostart_check.setChecked(config.app.start_with_windows)

        form = QFormLayout()
        form.addRow("每日更换时间", self.daily_time_edit)
        form.addRow("最晚更换时间", self.latest_time_edit)
        form.addRow("天气城市", self.city_edit)
        form.addRow("月度预算 (¥)", self.budget_edit)
        form.addRow(self.wallhaven_check)
        form.addRow(self.bing_check)
        form.addRow(self.manual_counts_check)
        form.addRow(self.autostart_check)

        self.save_button = QPushButton("保存")
        self.save_button.clicked.connect(self.save)
        self.status = QLabel("")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save_button)
        layout.addWidget(self.status)
        layout.addStretch(1)

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
                start_with_windows=self.autostart_check.isChecked(),
            ),
            sources=replace(
                self.config.sources,
                wallhaven_enabled=self.wallhaven_check.isChecked(),
                bing_enabled=self.bing_check.isChecked(),
            ),
            weather=replace(self.config.weather, city=self.city_edit.text().strip()),
            budget=replace(self.config.budget, monthly_cny=budget),
        )

    def _apply_autostart(self, wanted: bool) -> None:
        try:
            service = self.autostart or AutostartService(
                command=default_command(self.config_path)
            )
            if wanted and not service.enabled():
                service.enable()
            elif not wanted and service.enabled():
                service.disable()
        except Exception as exc:  # a locked registry must not lose the config
            logger.warning(f"autostart toggle failed: {exc}")

    def save(self) -> bool:
        updated = self.collect()
        if updated is None:
            return False
        save_config(self.config_path, updated)
        self._apply_autostart(updated.app.start_with_windows)
        self.config = updated
        self.status.setText("已保存")
        if self.on_saved is not None:
            self.on_saved(updated)
        return True
