from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .converter import (
    CancelCallback,
    ConversionCancelled,
    ConversionReport,
    OsvConversionError,
    convert_xtv_to_osv,
)


class ExistingOutputAction(str, Enum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    AUTO_RENAME = "auto_rename"
    CANCEL = "cancel"


class BatchStatus(str, Enum):
    QUEUED = "queued"
    CONVERTING = "converting"
    CONVERTED = "converted"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceRemoval(str, Enum):
    NOT_REQUESTED = "not_requested"
    TRASHED = "trashed"
    PERMANENTLY_DELETED = "permanently_deleted"
    RETAINED = "retained"


class BatchCancelled(RuntimeError):
    """Raised when the user cancels batch preparation."""


@dataclass(frozen=True)
class BatchItemPlan:
    source: Path
    output: Path
    overwrite: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class BatchItemResult:
    source: Path
    output: Path
    status: BatchStatus
    message: str = ""
    report: ConversionReport | None = None
    source_removal: SourceRemoval = SourceRemoval.NOT_REQUESTED


@dataclass(frozen=True)
class BatchSummary:
    results: tuple[BatchItemResult, ...]
    cancelled: bool = False

    def count(self, status: BatchStatus) -> int:
        return sum(result.status == status for result in self.results)

    @property
    def removed_sources(self) -> int:
        return sum(
            result.source_removal
            in {SourceRemoval.TRASHED, SourceRemoval.PERMANENTLY_DELETED}
            for result in self.results
        )


ProgressCallback = Callable[[int, int, int], None]
StateCallback = Callable[[int, BatchStatus, str], None]
Converter = Callable[..., ConversionReport]
TrashFunction = Callable[[str | Path], None]


def collect_xtv_files(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """Return unique existing XTV files in input order."""
    accepted: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file() or path.suffix.casefold() != ".xtv":
            continue
        absolute = path.absolute()
        key = _path_key(absolute)
        if key not in seen:
            accepted.append(absolute)
            seen.add(key)
    return tuple(accepted)


def plan_output_paths(
    sources: Iterable[str | Path],
    destination_dir: str | Path | None = None,
) -> tuple[BatchItemPlan, ...]:
    """Plan unique OSV destinations without considering existing files."""
    destination = (
        Path(destination_dir).expanduser().absolute()
        if destination_dir is not None
        else None
    )
    plans: list[BatchItemPlan] = []
    reserved: set[str] = set()

    for source in collect_xtv_files(sources):
        parent = destination if destination is not None else source.parent
        candidate = parent / f"{source.stem}.OSV"
        output = _next_unique_path(candidate, reserved, check_disk=False)
        reserved.add(_path_key(output))
        plans.append(BatchItemPlan(source=source, output=output))

    return tuple(plans)


def existing_output_conflicts(
    plans: Iterable[BatchItemPlan],
) -> tuple[BatchItemPlan, ...]:
    return tuple(plan for plan in plans if plan.output.exists())


def resolve_existing_outputs(
    plans: Iterable[BatchItemPlan],
    action: ExistingOutputAction,
) -> tuple[BatchItemPlan, ...]:
    """Apply one action to every destination that already exists."""
    if action is ExistingOutputAction.CANCEL:
        raise BatchCancelled("batch preparation cancelled")

    resolved: list[BatchItemPlan] = []
    plans_tuple = tuple(plans)
    reserved = {_path_key(plan.output) for plan in plans_tuple}

    for plan in plans_tuple:
        output = plan.output
        if output.exists():
            if action is ExistingOutputAction.SKIP:
                plan = BatchItemPlan(
                    source=plan.source,
                    output=output,
                    skip_reason="Output already exists",
                )
            elif action is ExistingOutputAction.OVERWRITE:
                plan = BatchItemPlan(
                    source=plan.source,
                    output=output,
                    overwrite=True,
                )
            elif action is ExistingOutputAction.AUTO_RENAME:
                reserved.discard(_path_key(output))
                output = _next_unique_path(
                    output,
                    reserved,
                    check_disk=True,
                )
                plan = BatchItemPlan(source=plan.source, output=output)

        reserved.add(_path_key(plan.output))
        resolved.append(plan)

    return tuple(resolved)


def run_batch(
    plans: Iterable[BatchItemPlan],
    *,
    delete_sources: bool = False,
    progress_callback: ProgressCallback | None = None,
    state_callback: StateCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    converter: Converter = convert_xtv_to_osv,
    trash_function: TrashFunction | None = None,
    trash_permission_error: type[BaseException] | None = None,
) -> BatchSummary:
    """Convert plans sequentially and continue after per-file failures."""
    results: list[BatchItemResult] = []
    cancelled = False

    for index, plan in enumerate(plans):
        if cancel_callback is not None and cancel_callback():
            cancelled = True
            break

        if plan.skip_reason:
            _emit_state(
                state_callback,
                index,
                BatchStatus.SKIPPED,
                plan.skip_reason,
            )
            results.append(
                BatchItemResult(
                    source=plan.source,
                    output=plan.output,
                    status=BatchStatus.SKIPPED,
                    message=plan.skip_reason,
                )
            )
            continue

        if plan.output.exists() and not plan.overwrite:
            message = "Output appeared after conflict checking; skipped"
            _emit_state(state_callback, index, BatchStatus.SKIPPED, message)
            results.append(
                BatchItemResult(
                    source=plan.source,
                    output=plan.output,
                    status=BatchStatus.SKIPPED,
                    message=message,
                )
            )
            continue

        _emit_state(state_callback, index, BatchStatus.CONVERTING, "Converting")
        try:
            report = converter(
                plan.source,
                plan.output,
                progress_callback=(
                    (
                        lambda processed, total, item_index=index: progress_callback(
                            item_index,
                            processed,
                            total,
                        )
                    )
                    if progress_callback is not None
                    else None
                ),
                cancel_callback=cancel_callback,
            )
            _validate_output(plan.output, report)
        except ConversionCancelled:
            message = "Cancelled"
            _emit_state(
                state_callback,
                index,
                BatchStatus.CANCELLED,
                message,
            )
            results.append(
                BatchItemResult(
                    source=plan.source,
                    output=plan.output,
                    status=BatchStatus.CANCELLED,
                    message=message,
                )
            )
            cancelled = True
            break
        except (OsvConversionError, OSError) as exc:
            message = str(exc)
            _emit_state(state_callback, index, BatchStatus.FAILED, message)
            results.append(
                BatchItemResult(
                    source=plan.source,
                    output=plan.output,
                    status=BatchStatus.FAILED,
                    message=message,
                )
            )
            continue
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            _emit_state(state_callback, index, BatchStatus.FAILED, message)
            results.append(
                BatchItemResult(
                    source=plan.source,
                    output=plan.output,
                    status=BatchStatus.FAILED,
                    message=message,
                )
            )
            continue

        removal = SourceRemoval.NOT_REQUESTED
        message = "Converted"
        if delete_sources:
            removal, removal_message = remove_source_after_success(
                plan.source,
                trash_function=trash_function,
                trash_permission_error=trash_permission_error,
            )
            if removal_message:
                message = f"{message}; {removal_message}"

        _emit_state(state_callback, index, BatchStatus.CONVERTED, message)
        results.append(
            BatchItemResult(
                source=plan.source,
                output=plan.output,
                status=BatchStatus.CONVERTED,
                message=message,
                report=report,
                source_removal=removal,
            )
        )

    return BatchSummary(tuple(results), cancelled=cancelled)


def remove_source_after_success(
    source: str | Path,
    *,
    trash_function: TrashFunction | None = None,
    trash_permission_error: type[BaseException] | None = None,
) -> tuple[SourceRemoval, str]:
    """Trash a source file, permanently deleting only when Trash is unavailable."""
    path = Path(source)
    if trash_function is None or trash_permission_error is None:
        from send2trash import TrashPermissionError, send2trash

        if trash_function is None:
            trash_function = send2trash
        if trash_permission_error is None:
            trash_permission_error = TrashPermissionError

    try:
        trash_function(path)
    except trash_permission_error:
        try:
            path.unlink()
        except OSError as exc:
            return SourceRemoval.RETAINED, f"source retained: {exc}"
        return (
            SourceRemoval.PERMANENTLY_DELETED,
            "source permanently deleted because Trash was unavailable",
        )
    except Exception as exc:
        return SourceRemoval.RETAINED, f"source retained: {exc}"

    return SourceRemoval.TRASHED, "source moved to Trash"


def _next_unique_path(
    candidate: Path,
    reserved: set[str],
    *,
    check_disk: bool,
) -> Path:
    if _path_key(candidate) not in reserved and (
        not check_disk or not candidate.exists()
    ):
        return candidate

    counter = 1
    while True:
        renamed = candidate.with_name(
            f"{candidate.stem} ({counter}){candidate.suffix}"
        )
        if _path_key(renamed) not in reserved and (
            not check_disk or not renamed.exists()
        ):
            return renamed
        counter += 1


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _validate_output(output: Path, report: ConversionReport) -> None:
    if not output.is_file():
        raise OsvConversionError("conversion did not create the output file")
    actual_size = output.stat().st_size
    if actual_size != report.output_bytes:
        raise OsvConversionError(
            f"output size changed after conversion: {actual_size} != "
            f"{report.output_bytes}"
        )


def _emit_state(
    callback: StateCallback | None,
    index: int,
    status: BatchStatus,
    message: str,
) -> None:
    if callback is not None:
        callback(index, status, message)
