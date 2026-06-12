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
ByteRange = tuple[int, int]


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
            metadata_sample_ranges = _find_metadata_sample_ranges_in_file(
                src,
                input_size,
            )
            src.seek(0)
            _transform_file(
                src,
                input_size,
                writer,
                stats,
                progress_callback,
                cancel_callback,
                metadata_sample_ranges,
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
                metadata_sample_ranges = _find_metadata_sample_ranges_in_file(
                    src,
                    input_size,
                )
                src.seek(0)
                _transform_file(
                    src,
                    input_size,
                    writer,
                    stats,
                    progress_callback,
                    cancel_callback,
                    metadata_sample_ranges,
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
    metadata_sample_ranges = _find_metadata_sample_ranges_in_bytes(data)
    output = b"".join(
        _transform_top_level_box(
            header,
            payload,
            stats,
            metadata_sample_ranges,
        )
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
    metadata_sample_ranges: tuple[ByteRange, ...] = (),
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
            writer.write(
                _transform_top_level_box(
                    header,
                    payload,
                    stats,
                    metadata_sample_ranges,
                )
            )
        else:
            writer.write(header.header_bytes)
            if header.box_type == b"mdat":
                _copy_payload_with_selective_replacements(
                    src,
                    writer,
                    header.payload_size,
                    stats,
                    metadata_sample_ranges,
                    input_offset=offset + header.header_size,
                    input_size=input_size,
                    progress_callback=progress_callback,
                    cancel_callback=cancel_callback,
                )
            else:
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
    metadata_sample_ranges: tuple[ByteRange, ...] = (),
) -> bytes:
    if header.box_type in TOP_LEVEL_REBUILD_BOX_TYPES:
        result = _transform_box(header.box_type, payload, stats)
        return _pack_box(header.box_type, result.data)
    if header.box_type == b"mdat":
        return header.header_bytes + _replace_markers_in_ranges(
            payload,
            header.offset + header.header_size,
            metadata_sample_ranges,
            stats,
        )
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


def _find_metadata_sample_ranges_in_file(
    src: BinaryIO,
    input_size: int,
) -> tuple[ByteRange, ...]:
    moov_payloads: list[bytes] = []
    mdat_ranges: list[ByteRange] = []
    offset = 0

    while offset < input_size:
        header = _read_box_header(src, offset, input_size)
        payload_start = offset + header.header_size
        if header.box_type == b"moov":
            payload = src.read(header.payload_size)
            if len(payload) != header.payload_size:
                raise OsvConversionError(
                    f"could not read payload for moov box at {offset}"
                )
            moov_payloads.append(payload)
        else:
            if header.box_type == b"mdat":
                mdat_ranges.append((payload_start, offset + header.size))
            src.seek(offset + header.size)
        offset += header.size

    return _find_metadata_sample_ranges(moov_payloads, mdat_ranges)


def _find_metadata_sample_ranges_in_bytes(
    data: bytes,
) -> tuple[ByteRange, ...]:
    moov_payloads: list[bytes] = []
    mdat_ranges: list[ByteRange] = []
    for header, payload in _iter_boxes(data):
        if header.box_type == b"moov":
            moov_payloads.append(payload)
        elif header.box_type == b"mdat":
            mdat_ranges.append(
                (
                    header.offset + header.header_size,
                    header.offset + header.size,
                )
            )
    return _find_metadata_sample_ranges(moov_payloads, mdat_ranges)


def _find_metadata_sample_ranges(
    moov_payloads: list[bytes],
    mdat_ranges: list[ByteRange],
) -> tuple[ByteRange, ...]:
    candidates: list[ByteRange] = []
    for moov_payload in moov_payloads:
        for header, trak_payload in _iter_boxes(moov_payload):
            if header.box_type != b"trak":
                continue
            sample_ranges = _metadata_ranges_from_track(trak_payload)
            candidates.extend(sample_ranges)

    valid_ranges = sorted({
        sample_range
        for sample_range in candidates
        if sample_range[0] < sample_range[1]
        and any(
            mdat_start <= sample_range[0] and sample_range[1] <= mdat_end
            for mdat_start, mdat_end in mdat_ranges
        )
    })
    if any(
        current[1] > following[0]
        for current, following in zip(valid_ranges, valid_ranges[1:])
    ):
        return ()
    return tuple(valid_ranges)


def _metadata_ranges_from_track(trak_payload: bytes) -> tuple[ByteRange, ...]:
    try:
        mdia_payload = _child_payload(trak_payload, b"mdia")
        minf_payload = _child_payload(mdia_payload, b"minf")
        stbl_payload = _child_payload(minf_payload, b"stbl")
        boxes = {
            header.box_type: payload
            for header, payload in _iter_boxes(stbl_payload)
        }
        xtmd_descriptions = _xtmd_description_indices(boxes[b"stsd"])
        if not xtmd_descriptions:
            return ()
        chunk_map = _parse_stsc(boxes[b"stsc"])
        sample_sizes = _parse_stsz(boxes[b"stsz"])
        chunk_offsets = _parse_chunk_offsets(boxes)
    except (KeyError, OsvConversionError, struct.error):
        return ()

    ranges: list[ByteRange] = []
    sample_index = 0
    map_index = 0
    for chunk_index, chunk_offset in enumerate(chunk_offsets, start=1):
        while (
            map_index + 1 < len(chunk_map)
            and chunk_map[map_index + 1][0] <= chunk_index
        ):
            map_index += 1
        first_chunk, samples_per_chunk, description_index = chunk_map[map_index]
        if first_chunk > chunk_index:
            return ()
        next_sample_index = sample_index + samples_per_chunk
        if next_sample_index > len(sample_sizes):
            return ()

        sample_offset = chunk_offset
        for sample_size in sample_sizes[sample_index:next_sample_index]:
            if description_index in xtmd_descriptions:
                ranges.append((sample_offset, sample_offset + sample_size))
            sample_offset += sample_size
        sample_index = next_sample_index

    if sample_index != len(sample_sizes):
        return ()
    return tuple(ranges)


def _child_payload(parent_payload: bytes, box_type: bytes) -> bytes:
    for header, payload in _iter_boxes(parent_payload):
        if header.box_type == box_type:
            return payload
    kind = box_type.decode("latin1")
    raise OsvConversionError(f"missing {kind} box")


def _xtmd_description_indices(stsd_payload: bytes) -> set[int]:
    if len(stsd_payload) < 8:
        raise OsvConversionError("stsd box is too short")
    entry_count = struct.unpack_from(">I", stsd_payload, 4)[0]
    indices: set[int] = set()
    offset = 8
    for index in range(1, entry_count + 1):
        if offset + 8 > len(stsd_payload):
            raise OsvConversionError("stsd entry extends past the end of the box")
        entry_size, entry_type = struct.unpack_from(">I4s", stsd_payload, offset)
        if entry_size < 8 or offset + entry_size > len(stsd_payload):
            raise OsvConversionError("invalid stsd entry size")
        if entry_type == b"xtmd":
            indices.add(index)
        offset += entry_size
    return indices


def _parse_stsc(payload: bytes) -> tuple[tuple[int, int, int], ...]:
    if len(payload) < 8:
        raise OsvConversionError("stsc box is too short")
    entry_count = struct.unpack_from(">I", payload, 4)[0]
    expected_size = 8 + entry_count * 12
    if len(payload) < expected_size or entry_count == 0:
        raise OsvConversionError("invalid stsc box")
    entries = tuple(
        struct.unpack_from(">III", payload, 8 + index * 12)
        for index in range(entry_count)
    )
    if entries[0][0] != 1 or any(
        first_chunk == 0
        or samples_per_chunk == 0
        or description_index == 0
        for first_chunk, samples_per_chunk, description_index in entries
    ):
        raise OsvConversionError("invalid stsc entry")
    if any(
        current[0] >= following[0]
        for current, following in zip(entries, entries[1:])
    ):
        raise OsvConversionError("stsc entries are not ordered")
    return entries


def _parse_stsz(payload: bytes) -> tuple[int, ...]:
    if len(payload) < 12:
        raise OsvConversionError("stsz box is too short")
    sample_size, sample_count = struct.unpack_from(">II", payload, 4)
    if sample_size:
        return (sample_size,) * sample_count
    expected_size = 12 + sample_count * 4
    if len(payload) < expected_size:
        raise OsvConversionError("invalid stsz box")
    return tuple(
        struct.unpack_from(">I", payload, 12 + index * 4)[0]
        for index in range(sample_count)
    )


def _parse_chunk_offsets(
    boxes: dict[bytes, bytes],
) -> tuple[int, ...]:
    if b"stco" in boxes:
        payload = boxes[b"stco"]
        entry_size = 4
        format_code = ">I"
    elif b"co64" in boxes:
        payload = boxes[b"co64"]
        entry_size = 8
        format_code = ">Q"
    else:
        raise OsvConversionError("missing stco or co64 box")

    if len(payload) < 8:
        raise OsvConversionError("chunk offset box is too short")
    entry_count = struct.unpack_from(">I", payload, 4)[0]
    expected_size = 8 + entry_count * entry_size
    if len(payload) < expected_size or entry_count == 0:
        raise OsvConversionError("invalid chunk offset box")
    return tuple(
        struct.unpack_from(format_code, payload, 8 + index * entry_size)[0]
        for index in range(entry_count)
    )


def _copy_payload_with_selective_replacements(
    src: BinaryIO,
    writer: _CountingWriter,
    payload_size: int,
    stats: ConversionStats,
    replacement_ranges: tuple[ByteRange, ...],
    *,
    input_offset: int,
    input_size: int,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    payload_end = input_offset + payload_size
    ranges = tuple(
        (start, end)
        for start, end in replacement_ranges
        if input_offset <= start < end <= payload_end
    )
    cursor = input_offset
    for start, end in ranges:
        _copy_payload_without_replacements(
            src,
            writer,
            start - cursor,
            input_offset=cursor,
            input_size=input_size,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        _copy_payload_with_marker_replacements(
            src,
            writer,
            end - start,
            stats,
            input_offset=start,
            input_size=input_size,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        cursor = end

    _copy_payload_without_replacements(
        src,
        writer,
        payload_end - cursor,
        input_offset=cursor,
        input_size=input_size,
        progress_callback=progress_callback,
        cancel_callback=cancel_callback,
    )


def _copy_payload_without_replacements(
    src: BinaryIO,
    writer: _CountingWriter,
    payload_size: int,
    *,
    input_offset: int,
    input_size: int,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    remaining = payload_size
    chunk_size = 1024 * 1024
    while remaining:
        _check_cancelled(cancel_callback)
        chunk = src.read(min(chunk_size, remaining))
        if not chunk:
            raise OsvConversionError(
                "input ended while reading an MP4 box payload"
            )
        writer.write(chunk)
        remaining -= len(chunk)
        _notify_progress(
            progress_callback,
            input_offset + payload_size - remaining,
            input_size,
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


def _replace_markers_in_ranges(
    payload: bytes,
    payload_offset: int,
    replacement_ranges: tuple[ByteRange, ...],
    stats: ConversionStats,
) -> bytes:
    output = bytearray(payload)
    payload_end = payload_offset + len(payload)
    for start, end in replacement_ranges:
        if not (payload_offset <= start < end <= payload_end):
            continue
        relative_start = start - payload_offset
        relative_end = end - payload_offset
        output[relative_start:relative_end] = _replace_markers(
            bytes(output[relative_start:relative_end]),
            stats,
        )
    return bytes(output)


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
