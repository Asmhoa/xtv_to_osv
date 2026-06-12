from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO, Callable


class OsvConversionError(RuntimeError):
    """Raised when an XTV file cannot be converted to the supported OSV layout."""


class ConversionCancelled(OsvConversionError):
    """Raised when an in-progress conversion is cancelled."""


ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


MARKER_REPLACEMENTS: tuple[tuple[bytes, bytes, str], ...] = (
    (b"XTRA 360", b"Osmo 360", "XTRA 360"),
    (b".XTV", b".OSV", ".XTV"),
)

DJMD_GMHD_BOX = bytes.fromhex(
    "00000054676d6864"
    "00000018676d696e00000000004080008000800000000000"
    "0000002c746578740001000000000000000000000000000000"
    "0100000000000000000000000000004000000000000008646a6d64"
)

CONTAINER_BOX_TYPES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"camd"}
MEDIA_INFO_HEADER_TYPES = {b"vmhd", b"smhd", b"hmhd", b"nmhd", b"gmhd"}
TOP_LEVEL_REBUILD_BOX_TYPES = {b"moov", b"camd"}


@dataclass
class ConversionStats:
    xtmd_entries_converted: int = 0
    gmhd_boxes_inserted: int = 0
    marker_replacements: dict[str, int] = field(
        default_factory=lambda: {name: 0 for _, _, name in MARKER_REPLACEMENTS}
    )

    def as_dict(self) -> dict:
        return {
            "xtmd_entries_converted": self.xtmd_entries_converted,
            "gmhd_boxes_inserted": self.gmhd_boxes_inserted,
            "marker_replacements": dict(self.marker_replacements),
        }


@dataclass(frozen=True)
class ConversionReport:
    input_path: str
    output_path: str
    dry_run: bool
    input_bytes: int
    output_bytes: int
    stats: ConversionStats

    def as_dict(self) -> dict:
        return {
            "input": self.input_path,
            "output": self.output_path,
            "dry_run": self.dry_run,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            **self.stats.as_dict(),
        }


@dataclass(frozen=True)
class _BoxHeader:
    offset: int
    size: int
    header_size: int
    box_type: bytes
    header_bytes: bytes

    @property
    def payload_size(self) -> int:
        return self.size - self.header_size


@dataclass(frozen=True)
class _TransformResult:
    data: bytes
    has_xtmd_stsd: bool = False


class _CountingWriter:
    def __init__(self, raw: BinaryIO | None = None) -> None:
        self.raw = raw
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        if self.raw is not None:
            return self.raw.write(data)
        return len(data)


def convert_xtv_to_osv(
    input_path: str | Path,
    output_path: str | Path,
    *,
    dry_run: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> ConversionReport:
    """Convert an XTV file to an OSV container without transcoding its media."""
    source = Path(input_path)
    target = Path(output_path)
    if not source.exists():
        raise OsvConversionError(f"input does not exist: {source}")
    if source.resolve() == target.resolve():
        raise OsvConversionError("input and output paths must be different")

    input_size = source.stat().st_size
    stats = ConversionStats()
    _check_cancelled(cancel_callback)
    _notify_progress(progress_callback, 0, input_size)

    if dry_run:
        writer = _CountingWriter()
        with source.open("rb") as src:
            _transform_file(
                src,
                input_size,
                writer,
                stats,
                progress_callback,
                cancel_callback,
            )
        _validate_stats(stats)
        _check_cancelled(cancel_callback)
        return ConversionReport(
            input_path=str(source),
            output_path=str(target),
            dry_run=True,
            input_bytes=input_size,
            output_bytes=writer.bytes_written,
            stats=stats,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            writer = _CountingWriter(tmp)
            with source.open("rb") as src:
                _transform_file(
                    src,
                    input_size,
                    writer,
                    stats,
                    progress_callback,
                    cancel_callback,
                )
        _validate_stats(stats)
        _check_cancelled(cancel_callback)
        os.replace(tmp_name, target)
        tmp_name = ""
        return ConversionReport(
            input_path=str(source),
            output_path=str(target),
            dry_run=False,
            input_bytes=input_size,
            output_bytes=target.stat().st_size,
            stats=stats,
        )
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


def transform_xtv_to_osv_bytes(data: bytes) -> tuple[bytes, ConversionStats]:
    """Transform an in-memory XTV container, primarily for testing."""
    stats = ConversionStats()
    output = b"".join(
        _transform_top_level_box(header, payload, stats)
        for header, payload in _iter_boxes(data)
    )
    _validate_stats(stats)
    return output, stats


def _transform_file(
    src: BinaryIO,
    input_size: int,
    writer: _CountingWriter,
    stats: ConversionStats,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    offset = 0
    while offset < input_size:
        _check_cancelled(cancel_callback)
        header = _read_box_header(src, offset, input_size)
        _notify_progress(
            progress_callback,
            offset + header.header_size,
            input_size,
        )
        if header.box_type in TOP_LEVEL_REBUILD_BOX_TYPES:
            payload = src.read(header.payload_size)
            if len(payload) != header.payload_size:
                kind = header.box_type.decode("latin1")
                raise OsvConversionError(
                    f"could not read payload for {kind} box at {offset}"
                )
            _check_cancelled(cancel_callback)
            _notify_progress(
                progress_callback,
                offset + header.size,
                input_size,
            )
            writer.write(_transform_top_level_box(header, payload, stats))
        else:
            writer.write(header.header_bytes)
            _copy_payload_with_marker_replacements(
                src,
                writer,
                header.payload_size,
                stats,
                input_offset=offset + header.header_size,
                input_size=input_size,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            )
        offset += header.size

    if offset != input_size:
        raise OsvConversionError("input ended before all MP4 boxes were read")
    _notify_progress(progress_callback, input_size, input_size)


def _transform_top_level_box(
    header: _BoxHeader,
    payload: bytes,
    stats: ConversionStats,
) -> bytes:
    if header.box_type in TOP_LEVEL_REBUILD_BOX_TYPES:
        result = _transform_box(header.box_type, payload, stats)
        return _pack_box(header.box_type, result.data)
    return header.header_bytes + _replace_markers(payload, stats)


def _transform_box(
    box_type: bytes,
    payload: bytes,
    stats: ConversionStats,
) -> _TransformResult:
    if box_type == b"stsd":
        return _transform_stsd(payload, stats)
    if box_type in CONTAINER_BOX_TYPES:
        return _transform_container(box_type, payload, stats)
    return _TransformResult(
        _replace_markers(payload, stats),
        has_xtmd_stsd=False,
    )


def _transform_container(
    box_type: bytes,
    payload: bytes,
    stats: ConversionStats,
) -> _TransformResult:
    parts: list[bytes] = []
    child_types: list[bytes] = []
    has_xtmd_stsd = False

    for child_header, child_payload in _iter_boxes(payload):
        child_result = _transform_box(
            child_header.box_type,
            child_payload,
            stats,
        )
        parts.append(_pack_box(child_header.box_type, child_result.data))
        child_types.append(child_header.box_type)
        has_xtmd_stsd = has_xtmd_stsd or child_result.has_xtmd_stsd

    if (
        box_type == b"minf"
        and has_xtmd_stsd
        and not any(
            child_type in MEDIA_INFO_HEADER_TYPES
            for child_type in child_types
        )
    ):
        parts.insert(0, DJMD_GMHD_BOX)
        stats.gmhd_boxes_inserted += 1

    return _TransformResult(
        b"".join(parts),
        has_xtmd_stsd=has_xtmd_stsd,
    )


def _transform_stsd(
    payload: bytes,
    stats: ConversionStats,
) -> _TransformResult:
    if len(payload) < 8:
        raise OsvConversionError("stsd box is too short")

    entry_count = struct.unpack_from(">I", payload, 4)[0]
    offset = 8
    output = bytearray(payload[:8])
    converted = False

    for _ in range(entry_count):
        if offset + 8 > len(payload):
            raise OsvConversionError("stsd entry extends past the end of the box")
        entry_size, entry_type = struct.unpack_from(">I4s", payload, offset)
        if entry_size < 8 or offset + entry_size > len(payload):
            raise OsvConversionError("invalid stsd entry size")

        replacement_type = b"djmd" if entry_type == b"xtmd" else entry_type
        if replacement_type != entry_type:
            converted = True
            stats.xtmd_entries_converted += 1

        entry_payload = _replace_markers(
            payload[offset + 8 : offset + entry_size],
            stats,
        )
        output += struct.pack(">I4s", entry_size, replacement_type)
        output += entry_payload
        offset += entry_size

    if offset != len(payload):
        output += _replace_markers(payload[offset:], stats)

    return _TransformResult(bytes(output), has_xtmd_stsd=converted)


def _iter_boxes(data: bytes) -> tuple[tuple[_BoxHeader, bytes], ...]:
    boxes: list[tuple[_BoxHeader, bytes]] = []
    offset = 0
    data_size = len(data)
    while offset < data_size:
        if offset + 8 > data_size:
            raise OsvConversionError(
                f"truncated MP4 box header at byte {offset}"
            )

        size32, box_type = struct.unpack_from(">I4s", data, offset)
        header_size = 8
        if size32 == 1:
            if offset + 16 > data_size:
                raise OsvConversionError(
                    f"truncated 64-bit MP4 box header at byte {offset}"
                )
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header_size = 16
        elif size32 == 0:
            size = data_size - offset
        else:
            size = size32

        if size < header_size:
            kind = box_type.decode("latin1")
            raise OsvConversionError(
                f"invalid {kind} box size at byte {offset}"
            )
        if offset + size > data_size:
            kind = box_type.decode("latin1")
            raise OsvConversionError(
                f"{kind} box extends past the available data"
            )

        header_bytes = data[offset : offset + header_size]
        payload = data[offset + header_size : offset + size]
        boxes.append(
            (
                _BoxHeader(
                    offset,
                    size,
                    header_size,
                    box_type,
                    header_bytes,
                ),
                payload,
            )
        )
        offset += size

    return tuple(boxes)


def _read_box_header(
    src: BinaryIO,
    offset: int,
    input_size: int,
) -> _BoxHeader:
    header = src.read(8)
    if len(header) != 8:
        raise OsvConversionError(
            f"truncated MP4 box header at byte {offset}"
        )

    size32, box_type = struct.unpack(">I4s", header)
    header_size = 8
    size = size32
    if size32 == 1:
        extra = src.read(8)
        if len(extra) != 8:
            raise OsvConversionError(
                f"truncated 64-bit MP4 box header at byte {offset}"
            )
        header += extra
        header_size = 16
        size = struct.unpack(">Q", extra)[0]
    elif size32 == 0:
        size = input_size - offset

    if size < header_size:
        kind = box_type.decode("latin1")
        raise OsvConversionError(
            f"invalid {kind} box size at byte {offset}"
        )
    if offset + size > input_size:
        kind = box_type.decode("latin1")
        raise OsvConversionError(
            f"{kind} box extends past the end of the file"
        )

    return _BoxHeader(
        offset=offset,
        size=size,
        header_size=header_size,
        box_type=box_type,
        header_bytes=header,
    )


def _copy_payload_with_marker_replacements(
    src: BinaryIO,
    writer: _CountingWriter,
    payload_size: int,
    stats: ConversionStats,
    *,
    input_offset: int = 0,
    input_size: int | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    max_marker = max(len(old) for old, _, _ in MARKER_REPLACEMENTS)
    tail = b""
    remaining = payload_size
    chunk_size = 1024 * 1024

    while remaining:
        _check_cancelled(cancel_callback)
        chunk = src.read(min(chunk_size, remaining))
        if not chunk:
            raise OsvConversionError(
                "input ended while reading an MP4 box payload"
            )
        remaining -= len(chunk)
        processed = input_offset + payload_size - remaining
        _notify_progress(
            progress_callback,
            processed,
            input_size if input_size is not None else processed,
        )
        data = tail + chunk
        if remaining:
            keep = min(max_marker - 1, len(data))
            writer.write(_replace_markers(data[:-keep], stats))
            tail = data[-keep:]
        else:
            writer.write(_replace_markers(data, stats))
            tail = b""

    if tail:
        writer.write(_replace_markers(tail, stats))


def _replace_markers(data: bytes, stats: ConversionStats) -> bytes:
    for old, new, name in MARKER_REPLACEMENTS:
        count = data.count(old)
        if count:
            stats.marker_replacements[name] += count
            data = data.replace(old, new)
    return data


def _pack_box(box_type: bytes, payload: bytes) -> bytes:
    size = len(payload) + 8
    if size <= 0xFFFFFFFF:
        return struct.pack(">I4s", size, box_type) + payload
    return struct.pack(">I4sQ", 1, box_type, size + 8) + payload


def _validate_stats(stats: ConversionStats) -> None:
    if stats.xtmd_entries_converted < 2:
        raise OsvConversionError(
            "no supported XTV camera metadata sample descriptions were found"
        )
    if stats.gmhd_boxes_inserted < 2:
        raise OsvConversionError(
            "could not add OSV general media headers for camera metadata tracks"
        )


def _check_cancelled(cancel_callback: CancelCallback | None) -> None:
    if cancel_callback is not None and cancel_callback():
        raise ConversionCancelled("conversion cancelled")


def _notify_progress(
    progress_callback: ProgressCallback | None,
    processed_bytes: int,
    total_bytes: int,
) -> None:
    if progress_callback is not None:
        progress_callback(processed_bytes, total_bytes)
