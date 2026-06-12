from __future__ import annotations

import configparser
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "xtra_to_osmo_build",
    ROOT / "scripts" / "build.py",
)
assert SPEC is not None and SPEC.loader is not None
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildTests(TestCase):
    def test_windows_spec_uses_absolute_output_and_bundles_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            destination = temp / ".build" / "pysidedeploy.spec"
            dist = temp / "dist"

            build._write_deployment_spec(
                destination,
                root=ROOT,
                dist_dir=dist,
                platform="win32",
                include_windows_runtime=True,
            )

            config = configparser.ConfigParser()
            config.read(destination, encoding="utf-8")
            self.assertEqual(
                Path(config["app"]["exec_directory"]),
                dist.resolve(),
            )
            self.assertEqual(
                Path(config["app"]["input_file"]),
                (ROOT / "app.py").resolve(),
            )
            self.assertIn(
                "--include-windows-runtime-dlls=yes",
                config["nuitka"]["extra_args"],
            )
            self.assertIn(
                "--windows-console-mode=disable",
                config["nuitka"]["extra_args"],
            )

    def test_windows_spec_can_build_without_bundled_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "pysidedeploy.spec"

            build._write_deployment_spec(
                destination,
                root=ROOT,
                dist_dir=Path(temp_dir) / "dist",
                platform="win32",
                include_windows_runtime=False,
            )

            config = configparser.ConfigParser()
            config.read(destination, encoding="utf-8")
            self.assertIn(
                "--include-windows-runtime-dlls=no",
                config["nuitka"]["extra_args"],
            )

    def test_build_requirements_include_onefile_compression(self) -> None:
        requirements = (ROOT / "requirements-build.txt").read_text(
            encoding="utf-8"
        )
        deployment = (ROOT / "packaging" / "pysidedeploy.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("zstandard==0.23.0", requirements)
        self.assertIn("zstandard==0.23.0", deployment)

    def test_smoke_failure_explains_windows_runtime_exit(self) -> None:
        result = subprocess.CompletedProcess(
            ["XtraToOsmo.exe"],
            2,
            stdout="",
            stderr="",
        )

        message = build._smoke_failure_message("startup failed", result)

        self.assertIn("exit code 2", message)
        self.assertIn("Visual C++ runtime", message)

    def test_deployment_spec_declares_required_qt_plugin_groups(self) -> None:
        config = configparser.ConfigParser()
        config.read(
            ROOT / "packaging" / "pysidedeploy.spec",
            encoding="utf-8",
        )

        plugins = set(config["qt"]["plugins"].split(","))

        self.assertEqual(
            plugins,
            {
                "platforms",
                "styles",
                "iconengines",
                "imageformats",
                "platforminputcontexts",
            },
        )
