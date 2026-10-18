# src/dailywallpaper/ui/history_page.py
"""Recent decisions with ratings and undo (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

THUMB = 96
HEADERS = ["日期", "缩略图", "标题", "总分", "我的评分"]
RATING_TEXT = {1: "喜欢", 0: "中性", -1: "不喜欢"}


class HistoryPage(QWidget):
    def __init__(self, ratings, on_undo, parent=None):
        super().__init__(parent)
        self.ratings = ratings
        self.on_undo = on_undo
        self.row_count = 0

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(lambda: self.on_undo())
        self.status = QLabel("")

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.undo_button)
        layout.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        rows = self.ratings.history()
        self.row_count = len(rows)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.table.setRowHeight(index, THUMB)
            self.table.setItem(index, 0, QTableWidgetItem(row.decision_date))
            self.table.setCellWidget(index, 1, self._thumb(row.local_path))
            self.table.setItem(index, 2, QTableWidgetItem(row.title))
            self.table.setItem(index, 3, QTableWidgetItem(f"{row.final_score:.1f}"))
            self.table.setItem(
                index, 4, QTableWidgetItem(RATING_TEXT.get(row.rating, ""))
            )

    @staticmethod
    def _thumb(path: str) -> QLabel:
        label = QLabel()
        if not path:
            return label
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return label
        label.setPixmap(
            pixmap.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio)
        )
        return label
