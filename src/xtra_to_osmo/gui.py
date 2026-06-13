from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from threading import Event
from typing import Iterable

from PySide6.QtCore import QMimeData, QObject, QSettings, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .batch import (
    BatchCancelled,
    BatchItemPlan,
    BatchStatus,
    BatchSummary,
    ExistingOutputAction,
    SourceRemoval,
    collect_xtv_files,
    existing_output_conflicts,
    plan_output_paths,
    resolve_existing_outputs,
    run_batch,
)


APP_NAME = "Xtra to Osmo"
ORGANIZATION_NAME = "XtraToOsmo"


class DropZone(QFrame):
    files_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("XTV file drop area")
        self.setAccessibleDescription(
            "Drop XTV files here or press Enter to choose files."
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(7)

        icon = QLabel("XTV  →  OSV")
        icon.setObjectName("dropMark")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop XTV files here")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("or choose files from your computer")
        subtitle.setObjectName("dropSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.choose_button = QPushButton("Choose XTV files")
        self.choose_button.setObjectName("secondaryButton")
        self.choose_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.choose_button.clicked.connect(self._choose_files)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(self.choose_button)
        button_row.addStretch()

        layout.addStretch()
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(7)
        layout.addLayout(button_row)
        layout.addStretch()

    @staticmethod
    def paths_from_mime(mime_data: QMimeData) -> tuple[Path, ...]:
        if not mime_data.hasUrls():
            return ()
        return tuple(
            Path(url.toLocalFile())
            for url in mime_data.urls()
            if url.isLocalFile()
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if collect_xtv_files(self.paths_from_mime(event.mimeData())):
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._clear_drag_state()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self.paths_from_mime(event.mimeData())
        accepted = collect_xtv_files(paths)
        self._clear_drag_state()
        if accepted:
            self.files_selected.emit(accepted)
            event.acceptProposedAction()
        else:
            event.ignore()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._choose_files()
            event.accept()
            return
        super().keyPressEvent(event)

    def _choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose XTV files",
            "",
            "XTV files (*.XTV *.xtv);;All files (*)",
        )
        if files:
            self.files_selected.emit(tuple(Path(path) for path in files))

    def _clear_drag_state(self) -> None:
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)


class ConversionWorker(QObject):
    progress = Signal(int, object, object)
    state_changed = Signal(int, str, str)
    finished = Signal(object)
    crashed = Signal(str)

    def __init__(
        self,
        plans: tuple[BatchItemPlan, ...],
        delete_sources: bool,
    ) -> None:
        super().__init__()
        self._plans = plans
        self._delete_sources = delete_sources
        self._cancel_event = Event()

    @Slot()
    def run(self) -> None:
        try:
            summary = run_batch(
                self._plans,
                delete_sources=self._delete_sources,
                progress_callback=self._emit_progress,
                state_callback=self._emit_state,
                cancel_callback=self._cancel_event.is_set,
            )
        except Exception as exc:
            self.crashed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(summary)

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def _emit_progress(
        self,
        index: int,
        processed: int,
        total: int,
    ) -> None:
        self.progress.emit(index, processed, total)

    def _emit_state(
        self,
        index: int,
        status: BatchStatus,
        message: str,
    ) -> None:
        self.state_changed.emit(index, status.value, message)


class MainWindow(QMainWindow):
    COLUMN_SOURCE = 0
    COLUMN_OUTPUT = 1
    COLUMN_SIZE = 2
    COLUMN_PROGRESS = 3
    COLUMN_RESULT = 4

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(780, 650)
        self.resize(940, 760)

        self._sources: list[Path] = []
        self._plans: tuple[BatchItemPlan, ...] = ()
        self._thread: QThread | None = None
        self._worker: ConversionWorker | None = None
        self._running = False
        self._close_when_finished = False

        self._build_ui()
        self._apply_style()
        self._restore_geometry()
        self._set_running(False)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Lossless bulk conversion for DJI Osmo 360 recordings")
        subtitle.setObjectName("appSubtitle")
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        header_row.addLayout(header_text)
        header_row.addStretch()
        version = QLabel(f"v{__version__}")
        version.setObjectName("versionLabel")
        header_row.addWidget(version, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header_row)

        self.drop_zone = DropZone()
        self.drop_zone.setMinimumHeight(185)
        self.drop_zone.files_selected.connect(self.add_files)
        layout.addWidget(self.drop_zone)

        options = QHBoxLayout()
        options.setSpacing(10)
        options_label = QLabel("Save")
        options_label.setObjectName("sectionLabel")
        options.addWidget(options_label)
        self.next_to_source_radio = QRadioButton("Next to source files")
        self.next_to_source_radio.setChecked(True)
        self.custom_destination_radio = QRadioButton("One folder")
        options.addWidget(self.next_to_source_radio)
        options.addWidget(self.custom_destination_radio)
        self.destination_edit = QLineEdit()
        self.destination_edit.setPlaceholderText("Choose an output folder")
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setEnabled(False)
        self.destination_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        options.addWidget(self.destination_edit, 1)
        self.destination_button = QPushButton("Browse")
        self.destination_button.setObjectName("secondaryButton")
        self.destination_button.setEnabled(False)
        options.addWidget(self.destination_button)
        layout.addLayout(options)

        safety_row = QHBoxLayout()
        safety_row.setContentsMargins(0, 0, 0, 2)
        self.delete_source_checkbox = QCheckBox("Delete source after success")
        self.delete_source_checkbox.setToolTip(
            "Moves converted XTV files to Trash. If Trash is unavailable, "
            "the app will permanently delete them after confirmation."
        )
        safety_row.addWidget(self.delete_source_checkbox)
        safety_row.addStretch()
        self.queue_count_label = QLabel("No files selected")
        self.queue_count_label.setObjectName("mutedLabel")
        safety_row.addWidget(self.queue_count_label)
        layout.addLayout(safety_row)

        queue_header = QHBoxLayout()
        queue_title = QLabel("Conversion queue")
        queue_title.setObjectName("sectionTitle")
        queue_header.addWidget(queue_title)
        queue_header.addStretch()
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("plainButton")
        queue_header.addWidget(self.clear_button)
        layout.addLayout(queue_header)

        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(
            ["Source", "Output", "Size", "Progress", "Result"]
        )
        self.queue_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.queue_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.queue_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.queue_table.setAlternatingRowColors(False)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setShowGrid(False)
        self.queue_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(
            self.COLUMN_SOURCE,
            QHeaderView.ResizeMode.Stretch,
        )
        header.setSectionResizeMode(
            self.COLUMN_OUTPUT,
            QHeaderView.ResizeMode.Stretch,
        )
        header.setSectionResizeMode(
            self.COLUMN_SIZE,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            self.COLUMN_PROGRESS,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(
            self.COLUMN_RESULT,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        layout.addWidget(self.queue_table, 1)

        progress_row = QHBoxLayout()
        self.status_label = QLabel("Add XTV files to begin")
        self.status_label.setObjectName("statusLabel")
        progress_row.addWidget(self.status_label)
        progress_row.addStretch()
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 1000)
        self.overall_progress.setValue(0)
        self.overall_progress.setTextVisible(False)
        self.overall_progress.setFixedWidth(180)
        progress_row.addWidget(self.overall_progress)
        self.convert_button = QPushButton("Convert files")
        self.convert_button.setObjectName("primaryButton")
        self.convert_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.convert_button.setDefault(True)
        progress_row.addWidget(self.convert_button)
        layout.addLayout(progress_row)

        self.next_to_source_radio.toggled.connect(self._destination_mode_changed)
        self.destination_button.clicked.connect(self._choose_destination)
        self.clear_button.clicked.connect(self.clear_files)
        self.convert_button.clicked.connect(self._convert_or_cancel)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f7f8fa;
                color: #171a21;
                font-size: 13px;
            }
            QLabel {
                background: transparent;
            }
            QLabel#appTitle {
                font-size: 26px;
                font-weight: 700;
                letter-spacing: -0.3px;
            }
            QLabel#appSubtitle, QLabel#mutedLabel, QLabel#versionLabel {
                color: #687080;
            }
            QLabel#versionLabel {
                font-size: 12px;
            }
            QLabel#sectionTitle {
                font-size: 15px;
                font-weight: 650;
            }
            QLabel#sectionLabel {
                color: #4c5565;
                font-weight: 650;
                margin-right: 4px;
            }
            QRadioButton {
                spacing: 6px;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
                background: #ffffff;
                border: 1px solid #8993a4;
                border-radius: 8px;
            }
            QRadioButton::indicator:checked {
                background: #2f7cf6;
                border-color: #1f64ca;
            }
            QRadioButton::indicator:disabled {
                background: #eef1f5;
                border-color: #b9c1cf;
            }
            QRadioButton::indicator:checked:disabled {
                background: #91a8ca;
                border-color: #7f98bd;
            }
            QFrame#dropZone {
                background: #ffffff;
                border: 2px dashed #b9c1cf;
                border-radius: 14px;
            }
            QFrame#dropZone[dragActive="true"] {
                background: #eef5ff;
                border-color: #2f7cf6;
            }
            QLabel#dropMark {
                color: #2f7cf6;
                font-size: 15px;
                font-weight: 700;
                letter-spacing: 1px;
            }
            QLabel#dropTitle {
                font-size: 19px;
                font-weight: 650;
            }
            QLabel#dropSubtitle {
                color: #707888;
            }
            QPushButton {
                min-height: 30px;
                padding: 0 13px;
                border-radius: 7px;
            }
            QPushButton#primaryButton {
                min-height: 38px;
                padding: 0 20px;
                background: #2f7cf6;
                color: white;
                border: 1px solid #2f7cf6;
                font-weight: 650;
            }
            QPushButton#primaryButton:hover {
                background: #246de0;
            }
            QPushButton#primaryButton:disabled {
                background: #aebbd0;
                border-color: #aebbd0;
            }
            QPushButton#secondaryButton {
                background: #ffffff;
                border: 1px solid #c8cfdb;
            }
            QPushButton#secondaryButton:hover {
                border-color: #8490a3;
            }
            QPushButton#plainButton {
                color: #4c5565;
                background: transparent;
                border: none;
                padding: 0 6px;
            }
            QLineEdit {
                min-height: 31px;
                padding: 0 9px;
                background: #ffffff;
                border: 1px solid #cdd3dd;
                border-radius: 7px;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #dce1e9;
                border-radius: 9px;
                outline: none;
                selection-background-color: #e8f1ff;
                selection-color: #171a21;
            }
            QTableWidget::item {
                padding: 7px 8px;
                border-bottom: 1px solid #edf0f4;
            }
            QHeaderView::section {
                background: #f1f3f6;
                color: #555f70;
                border: none;
                border-bottom: 1px solid #dce1e9;
                padding: 8px;
                font-weight: 650;
            }
            QProgressBar {
                min-height: 7px;
                max-height: 7px;
                background: #dfe4ec;
                border: none;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background: #2f7cf6;
                border-radius: 3px;
            }
            QLabel#statusLabel {
                color: #596273;
            }
            """
        )

    @Slot(object)
    def add_files(self, paths: Iterable[str | Path]) -> None:
        if self._running:
            return
        accepted = collect_xtv_files(paths)
        existing = {
            str(source.resolve(strict=False)).casefold()
            for source in self._sources
        }
        added = 0
        for source in accepted:
            key = str(source.resolve(strict=False)).casefold()
            if key not in existing:
                self._sources.append(source)
                existing.add(key)
                added += 1

        self._rebuild_queue()
        if added:
            self.status_label.setText(
                f"Ready to convert {len(self._sources)} "
                f"{'file' if len(self._sources) == 1 else 'files'}"
            )
        elif accepted:
            self.status_label.setText("Those files are already in the queue")
        else:
            self.status_label.setText("No valid XTV files were added")

    @Slot()
    def clear_files(self) -> None:
        if self._running:
            return
        self._sources.clear()
        self._plans = ()
        self.queue_table.setRowCount(0)
        self.overall_progress.setValue(0)
        self.queue_count_label.setText("No files selected")
        self.status_label.setText("Add XTV files to begin")
        self._set_running(False)

    def _destination_mode_changed(self, next_to_source: bool) -> None:
        custom = not next_to_source
        self.destination_edit.setEnabled(custom and not self._running)
        self.destination_button.setEnabled(custom and not self._running)
        self._rebuild_queue()

    def _choose_destination(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose output folder",
            self.destination_edit.text(),
        )
        if directory:
            self.destination_edit.setText(directory)
            self._rebuild_queue()

    def _destination(self) -> Path | None:
        if self.next_to_source_radio.isChecked():
            return None
        text = self.destination_edit.text().strip()
        return Path(text) if text else None

    def _rebuild_queue(self) -> None:
        destination = self._destination()
        self._plans = plan_output_paths(self._sources, destination)
        self.queue_table.setRowCount(len(self._plans))
        for row, plan in enumerate(self._plans):
            self._set_cell(row, self.COLUMN_SOURCE, str(plan.source))
            self._set_cell(row, self.COLUMN_OUTPUT, str(plan.output))
            self._set_cell(
                row,
                self.COLUMN_SIZE,
                _format_bytes(plan.source.stat().st_size),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            self._set_cell(row, self.COLUMN_PROGRESS, "0%")
            result = "Existing output" if plan.output.exists() else "Queued"
            self._set_cell(row, self.COLUMN_RESULT, result)

        count = len(self._plans)
        self.queue_count_label.setText(
            f"{count} {'file' if count == 1 else 'files'} selected"
            if count
            else "No files selected"
        )
        self._set_running(self._running)

    def _set_cell(
        self,
        row: int,
        column: int,
        text: str,
        alignment: Qt.AlignmentFlag | None = None,
    ) -> None:
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        if alignment is not None:
            item.setTextAlignment(alignment)
        self.queue_table.setItem(row, column, item)

    def _convert_or_cancel(self) -> None:
        if self._running:
            self._request_cancel()
        else:
            self._start_conversion()

    def _start_conversion(self) -> None:
        self._rebuild_queue()
        if not self._plans:
            self.status_label.setText("Add at least one XTV file")
            return
        if (
            self.custom_destination_radio.isChecked()
            and self._destination() is None
        ):
            self.status_label.setText("Choose an output folder")
            self.destination_button.setFocus()
            return

        conflicts = existing_output_conflicts(self._plans)
        if conflicts:
            action = self._ask_existing_output_action(conflicts)
            try:
                self._plans = resolve_existing_outputs(self._plans, action)
            except BatchCancelled:
                self.status_label.setText("Conversion cancelled")
                return
            self._sync_planned_outputs()

        if self.delete_source_checkbox.isChecked():
            answer = QMessageBox.warning(
                self,
                "Delete source files after conversion?",
                "Successfully converted XTV files will be moved to Trash or "
                "Recycle Bin.\n\nIf Trash is unavailable, the source will be "
                "permanently deleted. Failed or cancelled files are always kept.",
                QMessageBox.StandardButton.Cancel
                | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                self.status_label.setText("Conversion cancelled")
                return

        for row, plan in enumerate(self._plans):
            self._set_cell(row, self.COLUMN_PROGRESS, "0%")
            self._set_cell(
                row,
                self.COLUMN_RESULT,
                plan.skip_reason or "Queued",
            )
        self.overall_progress.setValue(0)
        self._set_running(True)
        self.status_label.setText(f"Converting 1 of {len(self._plans)}")

        self._thread = QThread(self)
        self._worker = ConversionWorker(
            self._plans,
            self.delete_source_checkbox.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.crashed.connect(self._on_worker_crashed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.crashed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _ask_existing_output_action(
        self,
        conflicts: tuple[BatchItemPlan, ...],
    ) -> ExistingOutputAction:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Existing OSV files")
        dialog.setText(
            f"{len(conflicts)} destination "
            f"{'file already exists' if len(conflicts) == 1 else 'files already exist'}."
        )
        dialog.setInformativeText("Choose one action for all existing files.")
        names = "\n".join(str(plan.output) for plan in conflicts)
        dialog.setDetailedText(names)
        overwrite = dialog.addButton(
            "Overwrite all",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        skip = dialog.addButton("Skip all", QMessageBox.ButtonRole.ActionRole)
        rename = dialog.addButton(
            "Auto-rename all",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel = dialog.addButton(
            QMessageBox.StandardButton.Cancel,
        )
        dialog.setDefaultButton(rename)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is overwrite:
            return ExistingOutputAction.OVERWRITE
        if clicked is skip:
            return ExistingOutputAction.SKIP
        if clicked is rename:
            return ExistingOutputAction.AUTO_RENAME
        if clicked is cancel:
            return ExistingOutputAction.CANCEL
        return ExistingOutputAction.CANCEL

    def _sync_planned_outputs(self) -> None:
        for row, plan in enumerate(self._plans):
            self._set_cell(row, self.COLUMN_OUTPUT, str(plan.output))

    @Slot(int, object, object)
    def _on_worker_progress(
        self,
        index: int,
        processed: int,
        total: int,
    ) -> None:
        fraction = processed / total if total else 1.0
        percent = max(0, min(100, round(fraction * 100)))
        self._set_cell(index, self.COLUMN_PROGRESS, f"{percent}%")
        overall = (index + fraction) / max(1, len(self._plans))
        self.overall_progress.setValue(round(overall * 1000))
        self.status_label.setText(
            f"Converting {index + 1} of {len(self._plans)}"
        )

    @Slot(int, str, str)
    def _on_worker_state(
        self,
        index: int,
        status_value: str,
        message: str,
    ) -> None:
        status = BatchStatus(status_value)
        labels = {
            BatchStatus.QUEUED: "Queued",
            BatchStatus.CONVERTING: "Converting",
            BatchStatus.CONVERTED: "Converted",
            BatchStatus.SKIPPED: "Skipped",
            BatchStatus.FAILED: "Failed",
            BatchStatus.CANCELLED: "Cancelled",
        }
        display = labels[status]
        if status in {BatchStatus.FAILED, BatchStatus.CANCELLED} and message:
            display = f"{display}: {message}"
        elif status is BatchStatus.SKIPPED:
            display = f"Skipped: {message}"
        self._set_cell(index, self.COLUMN_RESULT, display)
        if status in {
            BatchStatus.CONVERTED,
            BatchStatus.SKIPPED,
            BatchStatus.FAILED,
            BatchStatus.CANCELLED,
        }:
            self._set_cell(
                index,
                self.COLUMN_PROGRESS,
                "100%" if status is BatchStatus.CONVERTED else "—",
            )
            self.overall_progress.setValue(
                round(((index + 1) / max(1, len(self._plans))) * 1000)
            )

    @Slot(object)
    def _on_worker_finished(self, summary: BatchSummary) -> None:
        self._finish_worker()
        self.overall_progress.setValue(
            self.overall_progress.value() if summary.cancelled else 1000
        )
        converted = summary.count(BatchStatus.CONVERTED)
        failed = summary.count(BatchStatus.FAILED)
        skipped = summary.count(BatchStatus.SKIPPED)
        if summary.cancelled:
            self.status_label.setText(
                f"Cancelled after {converted} converted "
                f"{'file' if converted == 1 else 'files'}"
            )
        else:
            self.status_label.setText(
                f"Finished: {converted} converted, {failed} failed, "
                f"{skipped} skipped"
            )
        self._show_summary(summary)
        if self._close_when_finished:
            self.close()

    @Slot(str)
    def _on_worker_crashed(self, message: str) -> None:
        self._finish_worker()
        self.status_label.setText("Conversion stopped unexpectedly")
        QMessageBox.critical(
            self,
            "Conversion stopped",
            message,
        )
        if self._close_when_finished:
            self.close()

    def _show_summary(self, summary: BatchSummary) -> None:
        converted = summary.count(BatchStatus.CONVERTED)
        failed = summary.count(BatchStatus.FAILED)
        skipped = summary.count(BatchStatus.SKIPPED)
        cancelled = summary.count(BatchStatus.CANCELLED)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Conversion summary")
        dialog.setIcon(
            QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information
        )
        dialog.setText(
            f"{converted} converted · {skipped} skipped · "
            f"{failed} failed · {cancelled} cancelled"
        )
        dialog.setInformativeText(
            f"{summary.removed_sources} source "
            f"{'file was' if summary.removed_sources == 1 else 'files were'} "
            "moved to Trash or deleted."
        )
        details: list[str] = []
        for result in summary.results:
            details.append(
                f"{result.status.value.upper()}: {result.source}\n"
                f"  Output: {result.output}\n"
                f"  {result.message or 'No additional details'}"
            )
        dialog.setDetailedText("\n\n".join(details))
        dialog.exec()

    def _request_cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.request_cancel()
        self.convert_button.setText("Cancelling…")
        self.convert_button.setEnabled(False)
        self.status_label.setText("Cancelling after the current data chunk")

    def _finish_worker(self) -> None:
        self._set_running(False)
        self._worker = None
        self._thread = None

    def _set_running(self, running: bool) -> None:
        self._running = running
        has_files = bool(self._plans)
        self.drop_zone.setEnabled(not running)
        self.next_to_source_radio.setEnabled(not running)
        self.custom_destination_radio.setEnabled(not running)
        custom = self.custom_destination_radio.isChecked()
        self.destination_edit.setEnabled(not running and custom)
        self.destination_button.setEnabled(not running and custom)
        self.delete_source_checkbox.setEnabled(not running)
        self.clear_button.setEnabled(not running and has_files)
        self.convert_button.setEnabled(running or has_files)
        self.convert_button.setText("Cancel" if running else "Convert files")

    def _restore_geometry(self) -> None:
        settings = QSettings(ORGANIZATION_NAME, APP_NAME)
        geometry = settings.value("windowGeometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            answer = QMessageBox.question(
                self,
                "Cancel conversion and close?",
                "The current temporary output will be removed and source files "
                "will be kept.",
                QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_when_finished = True
                self._request_cancel()
            event.ignore()
            return

        QSettings(ORGANIZATION_NAME, APP_NAME).setValue(
            "windowGeometry",
            self.saveGeometry(),
        )
        event.accept()


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xtra-to-osmo-gui",
        description="Bulk convert DJI Osmo 360 XTV files to OSV.",
    )
    parser.add_argument("files", nargs="*", help="XTV files to add at startup")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the application version and exit",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    smoke_test = args.smoke_test or os.environ.get(
        "XTRA_TO_OSMO_SMOKE_TEST"
    ) == "1"
    if smoke_test and sys.platform != "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    app.setApplicationVersion(__version__)
    window = MainWindow()
    if args.files:
        window.add_files(args.files)
    if smoke_test:
        result_path = os.environ.get("XTRA_TO_OSMO_SMOKE_RESULT")
        if result_path:
            Path(result_path).write_text("ok\n", encoding="ascii")
        window.close()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
