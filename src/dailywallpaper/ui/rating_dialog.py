# src/dailywallpaper/ui/rating_dialog.py
"""Rating window (spec 11.3)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

QUICK_REASONS = [
    "太花哨",
    "太暗",
    "不喜欢动漫",
    "想要风景",
    "想要暗色",
    "想要简约",
    "想要人物",
    "其他",
]
COMMENT_MAX = 200
REASONS_PER_ROW = 4


class RatingDialog(QDialog):
    """Three states plus optional reasons; saving closes the dialog."""

    def __init__(self, service, wallpaper_id, parent=None):
        super().__init__(parent)
        self.service = service
        self.wallpaper_id = wallpaper_id
        self.chosen = None
        self.setWindowTitle("给这张壁纸打分")

        self.like_button = QPushButton("喜欢")
        self.neutral_button = QPushButton("中性")
        self.dislike_button = QPushButton("不喜欢")
        self.like_button.clicked.connect(lambda: self._save(1))
        self.neutral_button.clicked.connect(lambda: self._save(0))
        self.dislike_button.clicked.connect(lambda: self._save(-1))

        self.reason_buttons = {}
        grid = QGridLayout()
        for index, reason in enumerate(QUICK_REASONS):
            button = QPushButton(reason)
            button.setCheckable(True)
            self.reason_buttons[reason] = button
            grid.addWidget(button, index // REASONS_PER_ROW, index % REASONS_PER_ROW)

        self.comment_edit = QLineEdit()
        self.comment_edit.setMaxLength(COMMENT_MAX)
        self.comment_edit.setPlaceholderText("补充一句（可选）")

        self.hint_label = QLabel("已记录，会影响明天的推荐")

        buttons = QHBoxLayout()
        buttons.addWidget(self.like_button)
        buttons.addWidget(self.neutral_button)
        buttons.addWidget(self.dislike_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("快捷理由"))
        layout.addLayout(grid)
        layout.addWidget(self.comment_edit)
        layout.addLayout(buttons)
        layout.addWidget(self.hint_label)

    def selected_reasons(self) -> list[str]:
        return [reason for reason, button in self.reason_buttons.items() if button.isChecked()]

    def _save(self, rating: int) -> None:
        try:
            self.service.rate(
                self.wallpaper_id,
                rating,
                comment=self.comment_edit.text(),
                reasons=self.selected_reasons(),
            )
        except Exception as exc:  # a DB hiccup must not lose the dialog
            import logging

            logging.getLogger(__name__).warning(f"rating failed: {exc}")
            self.hint_label.setText(f"保存失败：{exc}")
            return
        self.chosen = rating
        self.accept()
