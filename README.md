# xtra-to-osmo

`xtra-to-osmo` losslessly converts DJI Osmo 360 `.XTV` files into native-style
`.OSV` containers. Encoded video and audio are copied without transcoding while
the DJI container metadata is rewritten to match the observed OSV layout.

The project includes a cross-platform desktop GUI for drag-and-drop batch
conversion and retains the original command-line interface.

## Requirements

- Python 3.11 or newer

The desktop app uses PySide6 and Send2Trash. Conversion does not require
FFmpeg.

## Install

```sh
python3 -m pip install -e .
```

## Desktop GUI

Launch the application from an editable install:

```sh
xtra-to-osmo-gui
```

Drop one or more XTV files into the window or use **Choose XTV files**. Outputs
are saved next to each source by default, or a shared destination folder can be
selected. Existing outputs are handled with one batch prompt.

**Delete source after success** is disabled by default. When enabled, sources
are removed only after their matching OSV file is created and validated. The
app first uses Trash or Recycle Bin. If that facility is unavailable, it uses
permanent deletion after displaying a confirmation before the batch starts.

## Command Line

```sh
xtra-to-osmo recording.XTV
xtra-to-osmo recording.XTV --output recording.OSV
xtra-to-osmo recording.XTV --dry-run
xtra-to-osmo recording.XTV --json
```

The default output is the input filename with an `.OSV` suffix. Existing output
files are protected unless `--force` is supplied.

The package can also be run as a module:

```sh
python3 -m xtra_to_osmo recording.XTV
```

## Python API

```python
from xtra_to_osmo import convert_xtv_to_osv

report = convert_xtv_to_osv("recording.XTV", "recording.OSV")
print(report.as_dict())
```

## Test

```sh
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
```

## Native Builds

Native builds use Python 3.12, Qt's `pyside6-deploy`, and Nuitka. Run the build
on the operating system being targeted:

```sh
python3.12 scripts/build.py
```

On Windows:

```powershell
py -3.12 scripts\build.py
```

The command creates an isolated `.build-venv`, installs pinned build
dependencies, builds the application, and runs a packaged smoke test. Output is
written to `dist/`:

- macOS: `XtraToOsmo.app`
- Windows: `XtraToOsmo.exe`
- Linux: `XtraToOsmo.bin`

For a fully self-contained Windows executable, install **Visual Studio 2022
Build Tools** with the **Desktop development with C++** workload. The build
script locates it through `vswhere`, activates the x64 developer environment,
and includes the licensed Microsoft runtime DLLs. Without Build Tools, the
build stops before compilation with installation guidance.

To deliberately build an executable that depends on the runtime already being
installed on every target computer, use:

```powershell
py -3.12 scripts\build.py --allow-external-vc-runtime
```

That fallback requires the
[Microsoft Visual C++ 2015-2022 Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
on the build computer and every target computer.

`zstandard` is installed by the build requirements so Nuitka onefile output is
compressed. A `dumpbin` warning means Visual Studio Build Tools were not
available for Qt's dependency scan; it is not the `dist` output-path failure.

The GitHub Actions workflow builds separate Apple Silicon and Intel macOS apps,
plus Windows x64 and Ubuntu 22.04-compatible Linux x64 artifacts. Tagged builds
are attached to a GitHub Release.

Unsigned downloads can trigger macOS Gatekeeper or Windows SmartScreen
warnings. Signing is enabled automatically when the corresponding repository
secrets are configured:

- macOS: `MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PASSWORD`,
  `MACOS_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, and
  `APPLE_APP_PASSWORD`
- Windows: `WINDOWS_CERTIFICATE` and `WINDOWS_CERTIFICATE_PASSWORD`
