from __future__ import annotations

import argparse
import configparser
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
    parser.add_argument(
        "--allow-external-vc-runtime",
        action="store_true",
        help=(
            "on Windows, allow an executable that requires the Microsoft "
            "Visual C++ Redistributable on the target computer"
        ),
    )
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 12):
        parser.error("Python 3.12 is required for reproducible desktop builds")

    if not args.no_bootstrap and not _is_build_environment():
        return _bootstrap_and_relaunch(
            args.dry_run,
            args.allow_external_vc_runtime,
        )

    deploy = _deploy_executable()
    if not deploy.exists():
        parser.error(
            "pyside6-deploy is not installed; run without --no-bootstrap "
            "or install requirements-build.txt"
        )

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    working_spec = BUILD_DIR / "pysidedeploy.spec"

    if not args.dry_run and DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment.setdefault(
        "NUITKA_CACHE_DIR",
        str(BUILD_DIR / "nuitka-cache"),
    )
    include_windows_runtime = (
        sys.platform == "win32" and not args.allow_external_vc_runtime
    )
    if sys.platform == "win32" and args.allow_external_vc_runtime:
        print(
            "WARNING: Building without bundled Microsoft runtime DLLs. "
            "The Microsoft Visual C++ 2015-2022 Redistributable must be "
            "installed on this and every target computer.",
            file=sys.stderr,
        )

    _write_deployment_spec(
        working_spec,
        include_windows_runtime=include_windows_runtime,
    )

    command = [str(deploy), "--config-file", str(working_spec), "--force"]
    if args.dry_run:
        command.append("--dry-run")
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
    if sys.platform == "win32":
        environment["QT_QPA_PLATFORM"] = "windows"
    else:
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")

    version_result = subprocess.run(
        [str(executable), "--version"],
        check=False,
        env=environment,
        timeout=30,
        capture_output=True,
        text=True,
    )
    if version_result.returncode:
        raise RuntimeError(
            _smoke_failure_message(
                "packaged executable could not start",
                version_result,
            )
        )

    marker = BUILD_DIR / "smoke-test.ok"
    marker.unlink(missing_ok=True)
    environment["XTRA_TO_OSMO_SMOKE_TEST"] = "1"
    environment["XTRA_TO_OSMO_SMOKE_RESULT"] = str(marker)
    smoke_result = subprocess.run(
        [str(executable)],
        check=False,
        env=environment,
        timeout=30,
        capture_output=True,
        text=True,
    )
    if smoke_result.returncode or not marker.is_file():
        raise RuntimeError(
            _smoke_failure_message(
                "packaged GUI startup smoke test failed",
                smoke_result,
            )
        )
    marker.unlink(missing_ok=True)


def _is_complete_artifact(artifact: Path) -> bool:
    if sys.platform == "darwin":
        executable_dir = artifact / "Contents" / "MacOS"
        return executable_dir.is_dir() and any(
            path.is_file() and os.access(path, os.X_OK)
            for path in executable_dir.iterdir()
        )
    return artifact.is_file() and artifact.stat().st_size > 0


def _smoke_failure_message(
    heading: str,
    result: subprocess.CompletedProcess[str],
) -> str:
    details = [
        f"{heading} with exit code {result.returncode}.",
        "On Windows, exit code 2 commonly means the Microsoft Visual C++ "
        "runtime is unavailable.",
    ]
    if result.stdout.strip():
        details.append(f"stdout:\n{result.stdout.strip()}")
    if result.stderr.strip():
        details.append(f"stderr:\n{result.stderr.strip()}")
    return "\n".join(details)


def _write_deployment_spec(
    destination: Path,
    *,
    root: Path = ROOT,
    dist_dir: Path = DIST_DIR,
    template: Path | None = None,
    platform: str | None = None,
    include_windows_runtime: bool = False,
) -> None:
    """Create a machine-local spec with absolute, normalized paths."""
    template = template or root / "packaging" / "pysidedeploy.spec"
    platform = platform or sys.platform
    config = configparser.ConfigParser()
    config.read(template, encoding="utf-8")
    config["app"]["project_dir"] = str(root.resolve())
    config["app"]["input_file"] = str((root / "app.py").resolve())
    config["app"]["exec_directory"] = str(dist_dir.resolve())

    extra_args = config["nuitka"].get("extra_args", "").split()
    if platform == "win32":
        extra_args.append("--windows-console-mode=disable")
        runtime_value = "yes" if include_windows_runtime else "no"
        extra_args.append(f"--include-windows-runtime-dlls={runtime_value}")
    config["nuitka"]["extra_args"] = " ".join(dict.fromkeys(extra_args))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as spec:
        config.write(spec)


def _bootstrap_and_relaunch(
    dry_run: bool,
    allow_external_vc_runtime: bool = False,
) -> int:
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
    if allow_external_vc_runtime:
        command.append("--allow-external-vc-runtime")
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
