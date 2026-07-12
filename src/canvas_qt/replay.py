"""Trace-replay transport bar (#3).

Re-animates a saved ``traces/*.jsonl`` run on the canvas by driving the SAME
pipeline a live Debug Run uses — the owner supplies three callbacks:

  * ``reset()``    — start a fresh run state (new overlay + cleared timeline),
  * ``feed(rec)``  — apply one trace record (overlay.consume + panel.on_trace),
  * ``after()``    — repaint the canvas / update status once per seek.

This widget owns only the records, the current position, and a QTimer for
auto-play; it knows nothing about the overlay or scene. Stepping forward feeds
records incrementally; stepping back (or scrubbing) resets and replays from the
start up to the target, since the overlay accumulates forward-only.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

# (label, timer interval in ms) — one auto-play step per interval.
_SPEEDS = [("0.5×", 1400), ("1×", 700), ("2×", 350),
           ("4×", 175), ("8×", 88)]


class ReplayBar(QWidget):
    """Play / step / scrub controls for replaying a saved trace."""

    def __init__(self, reset, feed, after, parent: QWidget | None = None):
        super().__init__(parent)
        self._reset = reset
        self._feed = feed
        self._after = after
        self._records: list = []
        self._count = 0
        self._index = -1            # index of the last-applied record (-1 = none)
        self._syncing = False       # guard so programmatic slider moves don't loop
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._tick)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)

        self.btn_start = QPushButton("⏮")        # jump to start
        self.btn_prev = QPushButton("◁")         # step back  (outline ≠ filled Play)
        self.btn_play = QPushButton("▶ Play")
        self.btn_next = QPushButton("▷")         # step forward
        self.btn_end = QPushButton("⏭")          # jump to end
        self.btn_start.setToolTip("Jump to start")
        self.btn_prev.setToolTip("Step back one event")
        self.btn_play.setToolTip("Play / pause")
        self.btn_next.setToolTip("Step forward one event")
        self.btn_end.setToolTip("Jump to end")
        for b in (self.btn_start, self.btn_prev, self.btn_next, self.btn_end):
            b.setMaximumWidth(34)
        self.btn_play.setMaximumWidth(72)
        self.btn_start.clicked.connect(lambda: self._user_seek(0))
        self.btn_prev.clicked.connect(lambda: self._user_seek(self._index - 1))
        self.btn_next.clicked.connect(lambda: self._user_seek(self._index + 1))
        self.btn_end.clicked.connect(lambda: self._user_seek(self._count - 1))
        self.btn_play.clicked.connect(self.toggle_play)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.setToolTip("Scrub through the run")
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderReleased.connect(self._on_slider_released)

        self.pos = QLabel("0 / 0")
        self.speed = QComboBox()
        for label, _ in _SPEEDS:
            self.speed.addItem(label)
        self.speed.setCurrentIndex(1)   # 1×
        self.speed.setToolTip("Playback speed")
        self.speed.currentIndexChanged.connect(self._on_speed)

        lay.addWidget(self.btn_start)
        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_next)
        lay.addWidget(self.btn_end)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.pos)
        lay.addWidget(self.speed)
        self._sync_ui()

    # ── public API ───────────────────────────────────────────────────────────
    def load(self, records) -> None:
        """Load a list of trace records and show the first event."""
        self.stop()
        self._records = list(records)
        self._count = len(self._records)
        self._index = -1
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, self._count - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._reset()
        if self._count:
            self.seek_to(0)
        else:
            self._after()
            self._sync_ui()

    def seek_to(self, target: int) -> None:
        """Render run state so that records[0..target] have been applied."""
        if not self._count:
            return
        target = max(0, min(int(target), self._count - 1))
        if target > self._index:
            for i in range(self._index + 1, target + 1):
                self._feed(self._records[i])
        elif target < self._index:
            self._reset()
            for i in range(target + 1):
                self._feed(self._records[i])
        self._index = target
        self._after()
        self._sync_ui()

    def play(self) -> None:
        if not self._count:
            return
        if self._index >= self._count - 1:
            self.seek_to(0)             # at the end → restart from the top
        self._timer.start()
        self._sync_ui()

    def pause(self) -> None:
        self._timer.stop()
        self._sync_ui()

    def stop(self) -> None:
        """Halt playback without touching the rendered state (used when a live
        Debug Run takes over)."""
        self._timer.stop()

    def toggle_play(self) -> None:
        self.pause() if self._timer.isActive() else self.play()

    # ── internals ────────────────────────────────────────────────────────────
    def _user_seek(self, target: int) -> None:
        self.pause()
        self.seek_to(target)

    def _on_slider(self, value: int) -> None:
        if self._syncing:
            return
        # Mid-drag, only update the cheap position readout; the heavy reset+replay
        # runs once on release (sliderReleased) so a long drag isn't O(n²). A
        # keyboard step or groove click (not "down") seeks immediately.
        if self.slider.isSliderDown():
            self.pos.setText(f"{value + 1} / {self._count}")
            return
        self.pause()
        self.seek_to(value)

    def _on_slider_released(self) -> None:
        # Capture the settled position BEFORE pause() — pause() runs _sync_ui,
        # which would snap the slider back to the (unchanged) current index.
        target = self.slider.value()
        self.pause()
        self.seek_to(target)

    def _on_speed(self, idx: int) -> None:
        if 0 <= idx < len(_SPEEDS):
            self._timer.setInterval(_SPEEDS[idx][1])

    def _tick(self) -> None:
        if self._index >= self._count - 1:
            self.pause()
            return
        self.seek_to(self._index + 1)

    def _sync_ui(self) -> None:
        self._syncing = True
        self.slider.setValue(max(0, self._index))
        self._syncing = False
        self.pos.setText(f"{self._index + 1} / {self._count}")
        self.btn_play.setText("⏸ Pause" if self._timer.isActive() else "▶ Play")
        has = self._count > 0
        at_start = self._index <= 0
        at_end = self._index >= self._count - 1
        self.btn_start.setEnabled(has and not at_start)
        self.btn_prev.setEnabled(has and not at_start)
        self.btn_next.setEnabled(has and not at_end)
        self.btn_end.setEnabled(has and not at_end)
        self.btn_play.setEnabled(has)
        self.slider.setEnabled(has)
