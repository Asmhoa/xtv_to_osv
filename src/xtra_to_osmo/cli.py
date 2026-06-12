from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .converter import ConversionReport, OsvConversionError, convert_xtv_to_osv


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".OSV")

    if not input_path.exists():
        parser.error(f"input does not exist: {input_path}")
    if output_path.exists() and not args.force and not args.dry_run:
        parser.error(f"output already exists: {output_path} (use --force to overwrite)")

    try:
        report = convert_xtv_to_osv(input_path, output_path, dry_run=args.dry_run)
    except OsvConversionError as exc:
        print(f"xtra-to-osmo: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_report(report)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Losslessly convert DJI Osmo 360 XTV files into native OSV containers."
    )
    parser.add_argument("input", help="input .XTV file")
    parser.add_argument(
        "-o",
        "--output",
        help="output .OSV path; defaults to INPUT with an .OSV suffix",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the output path if it already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and report the conversion without writing output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="print the conversion report as JSON",
    )
    return parser


def _print_report(report: ConversionReport) -> None:
    action = "Would convert" if report.dry_run else "Converted"
    print(f"{action} {report.input_path} -> {report.output_path}")
    print(f"xtmd->djmd entries: {report.stats.xtmd_entries_converted}")
    print(f"gmhd boxes inserted: {report.stats.gmhd_boxes_inserted}")
    replacements = ", ".join(
        f"{name}: {count}" for name, count in report.stats.marker_replacements.items()
    )
    print(f"marker replacements: {replacements}")
    print(f"bytes: {report.input_bytes} -> {report.output_bytes}")
