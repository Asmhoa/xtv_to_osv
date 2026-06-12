from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
from unittest import TestCase
from unittest.mock import patch

from xtra_to_osmo import (
    ConversionCancelled,
    OsvConversionError,
    convert_xtv_to_osv,
    transform_xtv_to_osv_bytes,
)
from xtra_to_osmo.cli import main
from xtra_to_osmo.converter import DJMD_GMHD_BOX


class ConverterTests(TestCase):
    def test_transform_rewrites_supported_xtv_metadata(self) -> None:
        source = _sample_xtv_bytes()

        converted, stats = transform_xtv_to_osv_bytes(source)

        self.assertEqual(stats.xtmd_entries_converted, 2)
        self.assertEqual(stats.gmhd_boxes_inserted, 2)
        self.assertEqual(
            len(converted),
            len(source) + 2 * len(DJMD_GMHD_BOX),
        )
        self.assertIn(
            b"video xtmd payload XTRA 360 /DCIM/CAM_0001_D.XTV",
            converted,
        )
        self.assertIn(
            b"unknown box Osmo 360 CAM_0001_D.OSV",
            converted,
        )
        self.assertEqual(converted.count(b"xtmd"), 1)
        self.assertEqual(converted.count(b"djmd"), 4)

    def test_transform_preserves_unmapped_mdat_payload(self) -> None:
        source = _sample_xtv_bytes()

        converted, _ = transform_xtv_to_osv_bytes(source)

        self.assertEqual(
            _top_level_payload(converted, "mdat"),
            _top_level_payload(source, "mdat"),
        )

    def test_transform_preserves_encoded_media_sample_hashes(self) -> None:
        source, samples = _sample_xtv_with_sample_tables()

        converted, _ = transform_xtv_to_osv_bytes(source)

        self._assert_sample_integrity(converted, samples)

    def test_file_conversion_preserves_encoded_media_sample_hashes(self) -> None:
        source, samples = _sample_xtv_with_sample_tables()
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.XTV"
            output_path = Path(temp_dir) / "output.OSV"
            input_path.write_bytes(source)

            convert_xtv_to_osv(input_path, output_path)

            converted = output_path.read_bytes()
            self._assert_sample_integrity(converted, samples)

    def test_transform_rejects_unsupported_container(self) -> None:
        source = _box("ftyp", b"isom") + _box(
            "moov",
            _box("uuid", b"no metadata"),
        )

        with self.assertRaises(OsvConversionError):
            transform_xtv_to_osv_bytes(source)

    def test_file_conversion_writes_output_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.XTV"
            output_path = Path(temp_dir) / "output.OSV"
            input_path.write_bytes(_sample_xtv_bytes())

            report = convert_xtv_to_osv(input_path, output_path)

            self.assertTrue(output_path.exists())
            self.assertFalse(report.dry_run)
            self.assertEqual(report.input_bytes, input_path.stat().st_size)
            self.assertEqual(report.output_bytes, output_path.stat().st_size)
            self.assertEqual(report.stats.xtmd_entries_converted, 2)

    def test_dry_run_reports_without_writing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.XTV"
            output_path = Path(temp_dir) / "input.OSV"
            input_path.write_bytes(_sample_xtv_bytes())

            report = convert_xtv_to_osv(
                input_path,
                output_path,
                dry_run=True,
            )

            self.assertFalse(output_path.exists())
            self.assertTrue(report.dry_run)
            self.assertGreater(report.output_bytes, report.input_bytes)

    def test_file_conversion_reports_monotonic_byte_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.XTV"
            output_path = Path(temp_dir) / "output.OSV"
            input_path.write_bytes(_sample_xtv_bytes(mdat_padding=2 * 1024 * 1024))
            updates: list[tuple[int, int]] = []

            convert_xtv_to_osv(
                input_path,
                output_path,
                progress_callback=lambda processed, total: updates.append(
                    (processed, total)
                ),
            )

            self.assertGreater(len(updates), 3)
            self.assertEqual(updates[0], (0, input_path.stat().st_size))
            self.assertEqual(updates[-1], (input_path.stat().st_size,) * 2)
            self.assertEqual(
                [processed for processed, _ in updates],
                sorted(processed for processed, _ in updates),
            )
            self.assertEqual(
                {total for _, total in updates},
                {input_path.stat().st_size},
            )

    def test_cancelled_conversion_removes_temporary_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.XTV"
            output_path = Path(temp_dir) / "output.OSV"
            input_path.write_bytes(_sample_xtv_bytes(mdat_padding=2 * 1024 * 1024))
            should_cancel = False

            def update_progress(processed: int, total: int) -> None:
                nonlocal should_cancel
                should_cancel = processed >= 1024 * 1024

            with self.assertRaises(ConversionCancelled):
                convert_xtv_to_osv(
                    input_path,
                    output_path,
                    progress_callback=update_progress,
                    cancel_callback=lambda: should_cancel,
                )

            self.assertTrue(input_path.exists())
            self.assertFalse(output_path.exists())
            self.assertEqual(
                list(Path(temp_dir).glob(f".{output_path.name}.*.tmp")),
                [],
            )

    def test_cli_defaults_to_osv_suffix_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "recording.XTV"
            output_path = input_path.with_suffix(".OSV")
            input_path.write_bytes(_sample_xtv_bytes())

            with patch("builtins.print") as print_mock:
                exit_code = main([str(input_path), "--json"])

            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            payload = json.loads(print_mock.call_args.args[0])
            self.assertEqual(payload["output"], str(output_path))
            self.assertEqual(payload["xtmd_entries_converted"], 2)

    def _assert_sample_integrity(
        self,
        converted: bytes,
        samples: dict[str, tuple[int, bytes]],
    ) -> None:
        for sample_name in ("video", "audio"):
            offset, payload = samples[sample_name]
            converted_payload = converted[offset : offset + len(payload)]
            self.assertEqual(
                hashlib.sha256(converted_payload).digest(),
                hashlib.sha256(payload).digest(),
                f"{sample_name} sample changed during conversion",
            )

        for sample_name in ("metadata_1", "metadata_2"):
            offset, payload = samples[sample_name]
            converted_payload = converted[offset : offset + len(payload)]
            self.assertEqual(
                converted_payload,
                payload.replace(b"XTRA 360", b"Osmo 360").replace(
                    b".XTV",
                    b".OSV",
                ),
            )


def _sample_xtv_bytes(*, mdat_padding: int = 0) -> bytes:
    mdat_payload = (
        b"video xtmd payload XTRA 360 /DCIM/CAM_0001_D.XTV"
        + b"\x00" * mdat_padding
    )
    return (
        _box("ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
        + _box("mdat", mdat_payload)
        + _box(
            "moov",
            _metadata_track()
            + _metadata_track()
            + _box("uuid", b"unknown box XTRA 360 CAM_0001_D.XTV"),
        )
    )


def _sample_xtv_with_sample_tables(
) -> tuple[bytes, dict[str, tuple[int, bytes]]]:
    ftyp = _box("ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    payloads = {
        "video": b"\x00\x00\x01\x26encoded XTRA 360 video .XTV bytes",
        "audio": b"\xff\xf1encoded XTRA 360 audio .XTV bytes",
        "metadata_1": b"XTRA 360 metadata path CAM_0001_D.XTV",
        "metadata_2": b"XTRA 360 metadata path CAM_0001_S.XTV",
    }
    mdat_payload = b"".join(payloads.values())
    payload_offset = len(ftyp) + 8
    samples: dict[str, tuple[int, bytes]] = {}
    for name, payload in payloads.items():
        samples[name] = (payload_offset, payload)
        payload_offset += len(payload)

    moov_payload = b"".join(
        (
            _sample_table_track("hvc1", *samples["video"]),
            _sample_table_track("mp4a", *samples["audio"]),
            _sample_table_track("xtmd", *samples["metadata_1"]),
            _sample_table_track("xtmd", *samples["metadata_2"]),
        )
    )
    source = ftyp + _box("mdat", mdat_payload) + _box("moov", moov_payload)
    return source, samples


def _metadata_track() -> bytes:
    return _box(
        "trak",
        _box(
            "mdia",
            _box(
                "minf",
                _box("dinf", b"url ") + _box("stbl", _stsd("xtmd")),
            ),
        ),
    )


def _sample_table_track(
    entry_type: str,
    chunk_offset: int,
    sample_payload: bytes,
) -> bytes:
    stsc = _box(
        "stsc",
        b"\x00" * 4
        + struct.pack(">I", 1)
        + struct.pack(">III", 1, 1, 1),
    )
    stsz = _box(
        "stsz",
        b"\x00" * 4
        + struct.pack(">II", 0, 1)
        + struct.pack(">I", len(sample_payload)),
    )
    stco = _box(
        "stco",
        b"\x00" * 4
        + struct.pack(">I", 1)
        + struct.pack(">I", chunk_offset),
    )
    return _box(
        "trak",
        _box(
            "mdia",
            _box(
                "minf",
                _box(
                    "stbl",
                    _stsd(entry_type) + stsc + stsz + stco,
                ),
            ),
        ),
    )


def _stsd(entry_type: str) -> bytes:
    entry = (
        struct.pack(">I4s", 16, entry_type.encode("ascii"))
        + b"\x00" * 8
    )
    return _box(
        "stsd",
        b"\x00\x00\x00\x00" + struct.pack(">I", 1) + entry,
    )


def _box(box_type: str, payload: bytes) -> bytes:
    return (
        struct.pack(">I4s", len(payload) + 8, box_type.encode("ascii"))
        + payload
    )


def _top_level_payload(data: bytes, expected_type: str) -> bytes:
    offset = 0
    while offset < len(data):
        size, box_type = struct.unpack_from(">I4s", data, offset)
        if box_type == expected_type.encode("ascii"):
            return data[offset + 8 : offset + size]
        offset += size
    raise AssertionError(f"missing {expected_type} box")
