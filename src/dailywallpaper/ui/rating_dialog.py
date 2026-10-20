# src/dailywallpaper/ui/rating_dialog.py
"""Rating window (spec 11.3)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from dailywallpaper.recommend.ratings import (
    MAX_RATING,
    MIN_RATING,
    QUICK_REASONS,
    rating_text,
    split_reasons,
)

COMMENT_MAX = 200
REASONS_PER_ROW = 4
SCORES_PER_ROW = 5
# Breathing room between the window and the screen edge, in pixels.
CORNER_MARGIN = 24


class RatingDialog(QDialog):
    """A 1-10 score plus optional reasons.

    Picking a score saves it but leaves the window open: the score is often
    chosen first and the reasons typed afterwards, and closing on the first
    click made that impossible.
    """

    def __init__(self, service, wallpaper_id, parent=None):
        super().__init__(parent)
        self.service = service
        self.wallpaper_id = wallpaper_id
        self.chosen = None
        self._placed = False
        self.setWindowTitle("给这张壁纸打分")

        info = service.conn.execute(
            "select title, local_path from wallpapers where id=?",
            (wallpaper_id,),
        ).fetchone()
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if info is not None and info["local_path"]:
            pixmap = QPixmap(info["local_path"])
            if not pixmap.isNull():
                self.preview_label.setPixmap(
                    pixmap.scaledToHeight(
                        140, Qt.TransformationMode.SmoothTransformation
                    )
                )
        self.title_label = QLabel(
            (info["title"] if info is not None else "") or "（无标题）"
        )
        self.title_label.setStyleSheet("font-weight: 600;")
        existing = service.rating_for(wallpaper_id)
        self.current_label = QLabel(
            f"当前评分：{rating_text(existing)}"
            if existing is not None
            else "还没有评分"
        )
        if existing is not None:
            row = service.conn.execute(
                "select comment from ratings where wallpaper_id=?",
                (wallpaper_id,),
            ).fetchone()
            # The comment column also holds the earlier quick reasons; pull
            # them back out so re-saving does not duplicate them.
            text, reasons = split_reasons(row["comment"] if row else "")
            self.comment_edit_text = text
            self.prefilled_reasons = reasons
        else:
            self.comment_edit_text = ""
            self.prefilled_reasons = []
        # The last score actually written; reason/comment edits re-save with
        # it, so the row always matches what the window shows.
        self._last_score = existing

        self.score_buttons: dict[int, QPushButton] = {}
        score_grid = QGridLayout()
        for index, score in enumerate(range(MIN_RATING, MAX_RATING + 1)):
            button = QPushButton(str(score))
            button.setCheckable(True)
            button.setChecked(existing == score)
            button.clicked.connect(
                lambda _checked=False, value=score: self._save(value)
            )
            self.score_buttons[score] = button
            score_grid.addWidget(
                button, index // SCORES_PER_ROW, index % SCORES_PER_ROW
            )

        self.reason_buttons = {}
        grid = QGridLayout()
        for index, reason in enumerate(QUICK_REASONS):
            button = QPushButton(reason)
            button.setCheckable(True)
            button.setChecked(reason in self.prefilled_reasons)
            self.reason_buttons[reason] = button
            grid.addWidget(button, index // REASONS_PER_ROW, index % REASONS_PER_ROW)
            # Connected after the initial check, or restoring a saved rating
            # would write it back before the window even opens.
            button.toggled.connect(self._resave)

        self.comment_edit = QLineEdit()
        self.comment_edit.setMaxLength(COMMENT_MAX)
        self.comment_edit.setPlaceholderText("补充一句（可选）")
        self.comment_edit.setText(self.comment_edit_text)
        self.comment_edit.editingFinished.connect(self._resave)

        self.hint_label = QLabel("点一个分数就保存，关掉窗口前可以继续改理由")
        self.close_button = QPushButton("完成")
        self.close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.current_label)
        layout.addWidget(QLabel("评分（1–10，越高越喜欢）"))
        layout.addLayout(score_grid)
        layout.addWidget(QLabel("快捷理由"))
        layout.addLayout(grid)
        layout.addWidget(self.comment_edit)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.close_button)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._placed:
            # A dialog without a parent lands in the middle of the screen;
            # the corner next to the tray is where the app is opened from.
            self._move_to_bottom_right()
            self._placed = True

    def _move_to_bottom_right(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(
            area.right() - self.width() - CORNER_MARGIN + 1,
            area.bottom() - self.height() - CORNER_MARGIN + 1,
        )

    def selected_reasons(self) -> list[str]:
        return [reason for reason, button in self.reason_buttons.items() if button.isChecked()]

    def _save(self, rating: int) -> None:
        if not self._persist(rating):
            return
        # The score row behaves like a radio group even though each button is
        # independently checkable.
        for value, button in self.score_buttons.items():
            if value != rating:
                button.setChecked(False)

    def _resave(self, *_args) -> None:
        """Re-write the saved row after a reason or comment edit."""
        if self._last_score is None:
            return
        self._persist(self._last_score)

    def _persist(self, rating: int) -> bool:
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
            return False
        self.chosen = rating
        self._last_score = rating
        self.current_label.setText(f"当前评分：{rating_text(rating)}")
        self.hint_label.setText(
            f"已保存：{rating} 分（改分数、理由或补一句话会自动更新）"
        )
        return True
