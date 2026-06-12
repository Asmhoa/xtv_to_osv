from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import TestCase

from xtra_to_osmo.batch import (
    BatchCancelled,
    BatchItemPlan,
    BatchStatus,
    ExistingOutputAction,
    SourceRemoval,
    collect_xtv_files,
    plan_output_paths,
    remove_source_after_success,
    resolve_existing_outputs,
    run_batch,
)
from xtra_to_osmo.converter import ConversionReport, ConversionStats


class FakeTrashPermissionError(PermissionError):
    pass


class BatchTests(TestCase):
    def test_collect_xtv_files_filters_directories_non_xtv_and_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xtv = root / "clip.XtV"
            xtv.write_bytes(b"x")
            other = root / "clip.mp4"
            other.write_bytes(b"x")

            files = collect_xtv_files([xtv, xtv, other, root])

            self.assertEqual(files, (xtv.absolute(),))

    def test_shared_destination_renames_same_named_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one" / "clip.XTV"
            second = root / "two" / "clip.XTV"
            destination = root / "output"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"x")
            second.write_bytes(b"x")

            plans = plan_output_paths([first, second], destination)

            self.assertEqual(plans[0].output, destination / "clip.OSV")
            self.assertEqual(plans[1].output, destination / "clip (1).OSV")

    def test_next_to_source_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.XTV"
            source.write_bytes(b"x")

            plans = plan_output_paths([source])

            self.assertEqual(plans[0].output, source.with_suffix(".OSV"))

    def test_existing_output_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "clip.XTV"
            output = root / "clip.OSV"
            source.write_bytes(b"x")
            output.write_bytes(b"existing")
            plans = (BatchItemPlan(source, output),)

            skipped = resolve_existing_outputs(
                plans,
                ExistingOutputAction.SKIP,
            )
            overwritten = resolve_existing_outputs(
                plans,
                ExistingOutputAction.OVERWRITE,
            )
            renamed = resolve_existing_outputs(
                plans,
                ExistingOutputAction.AUTO_RENAME,
            )

            self.assertEqual(skipped[0].skip_reason, "Output already exists")
            self.assertTrue(overwritten[0].overwrite)
            self.assertEqual(renamed[0].output, root / "clip (1).OSV")
            with self.assertRaises(BatchCancelled):
                resolve_existing_outputs(
                    plans,
                    ExistingOutputAction.CANCEL,
                )

    def test_remove_source_uses_trash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.XTV"
            source.write_bytes(b"x")
            trashed: list[Path] = []

            removal, message = remove_source_after_success(
                source,
                trash_function=lambda path: trashed.append(Path(path)),
                trash_permission_error=FakeTrashPermissionError,
            )

            self.assertEqual(removal, SourceRemoval.TRASHED)
            self.assertEqual(trashed, [source])
            self.assertIn("Trash", message)

    def test_remove_source_permanently_deletes_when_trash_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.XTV"
            source.write_bytes(b"x")

            def unavailable(path: str | Path) -> None:
                raise FakeTrashPermissionError

            removal, message = remove_source_after_success(
                source,
                trash_function=unavailable,
                trash_permission_error=FakeTrashPermissionError,
            )

            self.assertEqual(removal, SourceRemoval.PERMANENTLY_DELETED)
            self.assertFalse(source.exists())
            self.assertIn("permanently deleted", message)

    def test_remove_source_retains_file_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.XTV"
            source.write_bytes(b"x")

            def failed(path: str | Path) -> None:
                raise RuntimeError("trash failed")

            removal, message = remove_source_after_success(
                source,
                trash_function=failed,
                trash_permission_error=FakeTrashPermissionError,
            )

            self.assertEqual(removal, SourceRemoval.RETAINED)
            self.assertTrue(source.exists())
            self.assertIn("trash failed", message)

    def test_run_batch_continues_after_failure_and_skips_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources = [root / f"{name}.XTV" for name in ("one", "two", "three")]
            for source in sources:
                source.write_bytes(b"x")
            plans = (
                BatchItemPlan(sources[0], root / "one.OSV"),
                BatchItemPlan(
                    sources[1],
                    root / "two.OSV",
                    skip_reason="Output already exists",
                ),
                BatchItemPlan(sources[2], root / "three.OSV"),
            )

            def converter(source: Path, output: Path, **kwargs) -> ConversionReport:
                if source == sources[0]:
                    raise OSError("cannot convert")
                output.write_bytes(b"osv")
                return ConversionReport(
                    str(source),
                    str(output),
                    False,
                    source.stat().st_size,
                    output.stat().st_size,
                    ConversionStats(),
                )

            summary = run_batch(plans, converter=converter)

            self.assertEqual(
                [result.status for result in summary.results],
                [
                    BatchStatus.FAILED,
                    BatchStatus.SKIPPED,
                    BatchStatus.CONVERTED,
                ],
            )
