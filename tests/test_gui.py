from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest import TestCase

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from xtra_to_osmo.batch import BatchStatus
from xtra_to_osmo.gui import DropZone, MainWindow


class GuiTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()

    def test_safe_defaults(self) -> None:
        self.assertTrue(self.window.next_to_source_radio.isChecked())
        self.assertFalse(self.window.delete_source_checkbox.isChecked())
        self.assertFalse(self.window.destination_edit.isEnabled())

    def test_add_files_filters_invalid_and_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = root / "clip.XTV"
            invalid = root / "clip.mp4"
            valid.write_bytes(b"x")
            invalid.write_bytes(b"x")

            self.window.add_files([valid, invalid, valid])

            self.assertEqual(self.window.queue_table.rowCount(), 1)
            self.assertEqual(len(self.window._plans), 1)
            self.assertEqual(
                self.window.queue_table.item(0, self.window.COLUMN_RESULT).text(),
                "Queued",
            )

    def test_drop_zone_extracts_local_paths(self) -> None:
        mime = QMimeData()
        mime.setUrls(
            [
                QUrl.fromLocalFile("/tmp/one.XTV"),
                QUrl("https://example.com/two.XTV"),
            ]
        )

        self.assertEqual(
            DropZone.paths_from_mime(mime),
            (Path("/tmp/one.XTV"),),
        )

    def test_progress_and_queue_state_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.XTV"
            source.write_bytes(b"x" * 100)
            self.window.add_files([source])

            self.window._on_worker_state(
                0,
                BatchStatus.CONVERTING.value,
                "Converting",
            )
            self.window._on_worker_progress(0, 50, 100)

            self.assertEqual(
                self.window.queue_table.item(
                    0,
                    self.window.COLUMN_PROGRESS,
                ).text(),
                "50%",
            )
            self.assertEqual(self.window.overall_progress.value(), 500)
            self.assertEqual(
                self.window.queue_table.item(
                    0,
                    self.window.COLUMN_RESULT,
                ).text(),
                "Converting",
            )

    def test_cancel_requests_worker_and_updates_action(self) -> None:
        class FakeWorker:
            cancelled = False

            def request_cancel(self) -> None:
                self.cancelled = True

        worker = FakeWorker()
        self.window._worker = worker
        self.window._running = True
        self.window._request_cancel()

        self.assertTrue(worker.cancelled)
        self.assertFalse(self.window.convert_button.isEnabled())
        self.assertEqual(self.window.convert_button.text(), "Cancelling…")
        self.window._running = False
