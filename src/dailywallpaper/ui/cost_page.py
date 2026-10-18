# src/dailywallpaper/ui/cost_page.py
"""Monthly spend, cache hit rate and recent calls (spec 11.2)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

MODEL_HEADERS = ["模型", "调用", "花费"]
RECENT_HEADERS = ["时间", "模型", "用途", "花费", "缓存"]


class CostPage(QWidget):
    def __init__(self, cost_service, budget_cny, parent=None):
        super().__init__(parent)
        self.cost_service = cost_service
        self.budget_cny = budget_cny

        self.spent_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.stats_label = QLabel("")

        self.table = QTableWidget(0, len(MODEL_HEADERS))
        self.table.setHorizontalHeaderLabels(MODEL_HEADERS)
        self.table.verticalHeader().setVisible(False)

        self.recent_table = QTableWidget(0, len(RECENT_HEADERS))
        self.recent_table.setHorizontalHeaderLabels(RECENT_HEADERS)
        self.recent_table.verticalHeader().setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.spent_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.stats_label)
        layout.addWidget(QLabel("按模型"))
        layout.addWidget(self.table)
        layout.addWidget(QLabel("最近调用"))
        layout.addWidget(self.recent_table)
        self.refresh()

    def refresh(self) -> None:
        summary = self.cost_service.summary()
        self.spent_label.setText(
            f"本月 ¥{summary.spent:.2f} / 预算 ¥{self.budget_cny:.2f}"
        )
        percent = 0 if self.budget_cny <= 0 else int(
            min(100, summary.spent / self.budget_cny * 100)
        )
        self.progress.setValue(percent)
        self.stats_label.setText(
            f"调用 {summary.calls} 次 · 缓存命中率 {summary.cache_hit_rate() * 100:.0f}%"
        )

        self.table.setRowCount(len(summary.by_model))
        for index, row in enumerate(summary.by_model):
            self.table.setItem(index, 0, QTableWidgetItem(row.model))
            self.table.setItem(index, 1, QTableWidgetItem(str(row.calls)))
            self.table.setItem(index, 2, QTableWidgetItem(f"{row.cost:.4f}"))

        self.recent_table.setRowCount(len(summary.recent))
        for index, call in enumerate(summary.recent):
            self.recent_table.setItem(index, 0, QTableWidgetItem(call.ts))
            self.recent_table.setItem(index, 1, QTableWidgetItem(call.model))
            self.recent_table.setItem(index, 2, QTableWidgetItem(call.purpose))
            self.recent_table.setItem(index, 3, QTableWidgetItem(f"{call.cost:.4f}"))
            self.recent_table.setItem(
                index, 4, QTableWidgetItem("命中" if call.cache_hit else "调用")
            )
