from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
RELEASE_DIR = ROOT / "release"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive the native desktop build for release."
    )
    parser.add_argument(
        "label",
        choices=("macos-arm64", "macos-x64", "windows-x64", "linux-x64"),
    )
    args = parser.parse_args()

    version = _project_version()
    RELEASE_DIR.mkdir(exist_ok=True)
    base_name = f"xtra-to-osmo-{version}-{args.label}"

    if args.label.startswith("macos-"):
        source = DIST_DIR / "XtraToOsmo.app"
        output = RELEASE_DIR / f"{base_name}.zip"
        _require(source)
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(source),
                str(output),
            ],
            check=True,
        )
    elif args.label == "windows-x64":
        source = DIST_DIR / "XtraToOsmo.exe"
        output = RELEASE_DIR / f"{base_name}.zip"
        _require(source)
        shutil.make_archive(
            str(output.with_suffix("")),
            "zip",
            root_dir=DIST_DIR,
            base_dir=source.name,
        )
    else:
        source = DIST_DIR / "XtraToOsmo.bin"
        output = RELEASE_DIR / f"{base_name}.tar.gz"
        _require(source)
        with tarfile.open(output, "w:gz") as archive:
            archive.add(source, arcname=source.name)

    print(output)
    return 0


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as pyproject:
        return tomllib.load(pyproject)["project"]["version"]


def _require(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"build artifact not found: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
