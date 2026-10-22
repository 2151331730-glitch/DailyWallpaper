# src/dailywallpaper/ui/rating_dialog.py
"""Rating window: a 1-10 score plus one optional sentence."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from dailywallpaper.analysis.vision import KEYRING_ACCOUNT, KEYRING_SERVICE
from dailywallpaper.recommend.ratings import MAX_RATING, MIN_RATING, rating_text

COMMENT_MAX = 200
SCORES_PER_ROW = 5
# Breathing room between the window and the screen edge, in pixels.
CORNER_MARGIN = 24


class _IntentWorker(QObject):
    """Classify one sentence off the UI thread; the model call takes seconds."""

    done = Signal(object)

    def __init__(self, text: str, api_key: str):
        super().__init__()
        self.text = text
        self.api_key = api_key

    def run(self) -> None:
        from dailywallpaper.recommend.intents import classify_note

        self.done.emit(classify_note(self.text, self.api_key))


def _describe(intent: dict) -> str:
    if intent.get("kind") == "search":
        query = intent.get("query_zh") or intent.get("query_en") or ""
        return (
            f"已识别为搜索：想看「{query}」\n"
            "点「换一张」立即应用；不点则明天自动换图时生效"
        )
    tags = intent.get("tags") or []
    if not tags:
        return "已记录这条评语"
    want = "想要" if int(intent.get("polarity") or 1) > 0 else "不想要"
    return f"已识别：{want} " + "、".join(tags)


class RatingDialog(QDialog):
    """A 1-10 score plus one free sentence.

    Picking a score saves it but leaves the window open: the score is often
    chosen first and the sentence typed afterwards, and closing on the first
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
            self.comment_edit_text = (row["comment"] if row else "") or ""
        else:
            self.comment_edit_text = ""
        # The last score actually written; sentence edits re-save with it, so
        # the row always matches what the window shows.
        self._last_score = existing
        # The last sentence already classified; re-saving the same text must
        # not fire another model call.
        self._last_classified = ""
        self._intent_worker = None

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

        self.comment_edit = QLineEdit()
        self.comment_edit.setMaxLength(COMMENT_MAX)
        self.comment_edit.setPlaceholderText(
            "补充一句（可选，比如“太暗了”或“想看足球”）"
        )
        self.comment_edit.setText(self.comment_edit_text)
        self.comment_edit.editingFinished.connect(self._resave)

        self.intent_label = QLabel("")
        self.intent_label.setWordWrap(True)
        self.intent_label.setStyleSheet("color:#2563eb;font-size:11px;")
        self.hint_label = QLabel("点一个分数就保存，关掉窗口前可以继续补一句话")
        self.close_button = QPushButton("完成")
        self.close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.current_label)
        layout.addWidget(QLabel("评分（1–10，越高越喜欢）"))
        layout.addLayout(score_grid)
        layout.addWidget(self.comment_edit)
        layout.addWidget(self.intent_label)
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

    def _save(self, rating: int) -> None:
        if not self._persist(rating):
            return
        # The score row behaves like a radio group even though each button is
        # independently checkable.
        for value, button in self.score_buttons.items():
            if value != rating:
                button.setChecked(False)

    def _resave(self, *_args) -> None:
        """Re-write the saved row after a comment edit."""
        if self._last_score is None:
            return
        self._persist(self._last_score)

    def _persist(self, rating: int) -> bool:
        try:
            self.service.rate(
                self.wallpaper_id,
                rating,
                comment=self.comment_edit.text(),
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
            f"已保存：{rating} 分（改分数或补一句话会自动更新）"
        )
        self._maybe_classify()
        return True

    def _maybe_classify(self) -> None:
        """Classify a new sentence in the background; failures stay silent."""
        text = self.comment_edit.text().strip()
        if not text or text == self._last_classified:
            return
        import keyring

        api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if not api_key:
            # Do not mark it classified: a key added later should still work.
            return
        self._last_classified = text
        worker = _IntentWorker(text, api_key)
        worker.done.connect(self._on_intent)
        # Keep a reference: a worker that goes out of scope cancels nothing,
        # but the signal must still have a live receiver.
        self._intent_worker = worker
        threading.Thread(target=worker.run, daemon=True).start()

    def _on_intent(self, intent) -> None:
        if not intent:
            # Silence looked like success: say the sentence was saved but
            # could not be understood.
            self.intent_label.setText(
                "没识别出来；这句评语已保存，但不会影响推荐"
            )
            return
        from dailywallpaper.recommend.intents import store

        try:
            # Store the sentence that was actually classified, not a newer
            # edit the user made while the model was thinking.
            store(
                self.service.conn,
                intent,
                self.wallpaper_id,
                self._last_classified,
            )
        except Exception as exc:  # a full database must not break the dialog
            import logging

            logging.getLogger(__name__).warning(f"intent not stored: {exc}")
            return
        self.intent_label.setText(_describe(intent))
