from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]
BUILD_ENV = ROOT / ".build-venv"
BUILD_DIR = ROOT / ".build"
DIST_DIR = ROOT / "dist"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the native XtraToOsmo desktop artifact."
    )
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="use the active Python environment instead of .build-venv",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the pyside6-deploy command without compiling",
    )
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 12):
        parser.error("Python 3.12 is required for reproducible desktop builds")

    if not args.no_bootstrap and not _is_build_environment():
        return _bootstrap_and_relaunch(args.dry_run)

    deploy = _deploy_executable()
    if not deploy.exists():
        parser.error(
            "pyside6-deploy is not installed; run without --no-bootstrap "
            "or install requirements-build.txt"
        )

    BUILD_DIR.mkdir(exist_ok=True)
    working_spec = BUILD_DIR / "pysidedeploy.spec"
    shutil.copyfile(
        ROOT / "packaging" / "pysidedeploy.spec",
        working_spec,
    )

    if not args.dry_run and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    command = [str(deploy), "--config-file", str(working_spec), "--force"]
    if args.dry_run:
        command.append("--dry-run")
    environment = os.environ.copy()
    environment.setdefault(
        "NUITKA_CACHE_DIR",
        str(BUILD_DIR / "nuitka-cache"),
    )
    subprocess.run(
        command,
        cwd=BUILD_DIR,
        check=True,
        env=environment,
    )

    if args.dry_run:
        return 0

    artifact = native_artifact_path()
    if not _is_complete_artifact(artifact):
        raise RuntimeError(f"deployment did not create {artifact}")
    smoke_test(artifact)
    print(f"Built {artifact}")
    return 0


def native_artifact_path() -> Path:
    if sys.platform == "darwin":
        return DIST_DIR / "XtraToOsmo.app"
    if sys.platform == "win32":
        return DIST_DIR / "XtraToOsmo.exe"
    return DIST_DIR / "XtraToOsmo.bin"


def smoke_test(artifact: Path) -> None:
    executable = artifact
    if sys.platform == "darwin":
        candidates = tuple(
            path
            for path in (artifact / "Contents" / "MacOS").iterdir()
            if path.is_file() and os.access(path, os.X_OK)
        )
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one app executable, found {len(candidates)}"
            )
        executable = candidates[0]

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    subprocess.run(
        [str(executable), "--smoke-test"],
        check=True,
        env=environment,
        timeout=30,
    )


def _is_complete_artifact(artifact: Path) -> bool:
    if sys.platform == "darwin":
        executable_dir = artifact / "Contents" / "MacOS"
        return executable_dir.is_dir() and any(
            path.is_file() and os.access(path, os.X_OK)
            for path in executable_dir.iterdir()
        )
    return artifact.is_file() and artifact.stat().st_size > 0


def _bootstrap_and_relaunch(dry_run: bool) -> int:
    if not BUILD_ENV.exists():
        venv.EnvBuilder(with_pip=True).create(BUILD_ENV)
    python = _environment_python(BUILD_ENV)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "requirements-build.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-build-isolation",
            "--no-deps",
            "-e",
            str(ROOT),
        ],
        check=True,
    )
    command = [str(python), str(Path(__file__).resolve()), "--no-bootstrap"]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, cwd=ROOT).returncode


def _is_build_environment() -> bool:
    return Path(sys.prefix).resolve() == BUILD_ENV.resolve()


def _environment_python(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _deploy_executable() -> Path:
    scripts = Path(sys.executable).parent
    if sys.platform == "win32":
        return scripts / "pyside6-deploy.exe"
    return scripts / "pyside6-deploy"


if __name__ == "__main__":
    raise SystemExit(main())
