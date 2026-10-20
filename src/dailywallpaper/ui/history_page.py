# src/dailywallpaper/ui/history_page.py
"""Recent decisions with ratings and undo (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dailywallpaper.recommend.ratings import rating_text

THUMB = 96
HEADERS = ["日期", "缩略图", "标题", "总分", "我的评分"]


class HistoryPage(QWidget):
    def __init__(self, ratings, on_undo, on_rate=None, parent=None):
        super().__init__(parent)
        self.ratings = ratings
        self.on_undo = on_undo
        self.on_rate = on_rate
        self.row_count = 0
        self._wallpaper_ids: list[int] = []

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.itemSelectionChanged.connect(self._update_rate_button)
        self.table.cellDoubleClicked.connect(self._rate_row)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(lambda: self.on_undo())
        self.rate_button = QPushButton("评分 / 改评分")
        self.rate_button.setEnabled(False)
        self.rate_button.clicked.connect(self._rate_selected)
        self.status = QLabel("")

        buttons = QHBoxLayout()
        buttons.addWidget(self.undo_button)
        buttons.addWidget(self.rate_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        rows = self.ratings.history()
        self.row_count = len(rows)
        self._wallpaper_ids = [row.wallpaper_id for row in rows]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.table.setRowHeight(index, THUMB)
            self.table.setItem(index, 0, QTableWidgetItem(row.decision_date))
            self.table.setCellWidget(index, 1, self._thumb(row.local_path))
            self.table.setItem(index, 2, QTableWidgetItem(row.title))
            self.table.setItem(index, 3, QTableWidgetItem(f"{row.final_score:.1f}"))
            self.table.setItem(
                index, 4, QTableWidgetItem(rating_text(row.rating))
            )
        self.table.clearSelection()
        self._update_rate_button()

    def _selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _update_rate_button(self) -> None:
        row = self._selected_row()
        self.rate_button.setEnabled(
            self.on_rate is not None and 0 <= row < len(self._wallpaper_ids)
        )

    def _rate_selected(self) -> None:
        self._rate_row(self._selected_row())

    def _rate_row(self, row: int) -> None:
        if (
            self.on_rate is None
            or row < 0
            or row >= len(self._wallpaper_ids)
        ):
            return
        self.on_rate(self._wallpaper_ids[row])

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
