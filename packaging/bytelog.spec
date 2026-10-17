# PyInstaller spec for ByteLog: two windowed executables sharing one _internal folder.
#
#   dist/ByteLog/ByteLog.exe           dashboard (PySide6, pyqtgraph, reportlab)
#   dist/ByteLog/ByteLogCollector.exe  headless collector (no Qt, no numpy)
#   dist/ByteLog/_internal/            shared Python runtime, libraries and assets
#
# One-folder (not one-file) so the long-running collector never unpacks ~100 MB of Qt into
# %TEMP% and both programs share a single copy of their dependencies.
# Build with packaging/build.ps1, or: uv run pyinstaller packaging/bytelog.spec
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - injected by PyInstaller
SRC = ROOT / "src"
ASSETS = SRC / "bytelog" / "assets"
ICON = str(ASSETS / "icon.ico")

VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"', (SRC / "bytelog" / "__init__.py").read_text("utf-8")
).group(1)


def version_info(description: str, exe_name: str) -> VSVersionInfo:
    parts = [int(p) for p in re.findall(r"\d+", VERSION)[:4]]
    numbers = tuple(parts + [0] * (4 - len(parts)))
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "ByteLog"),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", exe_name),
                            StringStruct("OriginalFilename", f"{exe_name}.exe"),
                            StringStruct("ProductName", "ByteLog"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


# paths.resource_path() expects <_MEIPASS>/bytelog/assets in frozen builds.
datas = [(str(ASSETS), "bytelog/assets")] + collect_data_files("tzdata")

# Windows-Toasts talks to WinRT through per-namespace extension modules; the platform layer
# may import it lazily, so list it explicitly.
toast_imports = ["windows_toasts", *collect_submodules("winrt")]
toast_binaries = collect_dynamic_libs("winrt")

# The collector loads optional steps (rollup, alerts, retention) and the dashboard loads its
# pages by name at runtime, which PyInstaller cannot see, so bundle every bytelog module.
# The collector gets everything except the Qt UI and the reportlab exporters.
QT_ONLY = ("bytelog.ui", "bytelog.reports")
dashboard_modules = collect_submodules("bytelog")
collector_modules = collect_submodules("bytelog", filter=lambda name: not name.startswith(QT_ONLY))

COMMON_EXCLUDES = ["tkinter", "_tkinter", "unittest", "pydoc_data", "pytest", "_pytest"]
QT_STACK = ["PySide6", "shiboken6", "pyqtgraph", "numpy"]

dashboard = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_dashboard.py")],
    pathex=[str(SRC)],
    binaries=toast_binaries,
    datas=datas,
    hiddenimports=[*toast_imports, "tzlocal", *dashboard_modules],
    excludes=COMMON_EXCLUDES,
    noarchive=False,
)

collector = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry_collector.py")],
    pathex=[str(SRC)],
    binaries=toast_binaries,
    datas=datas,
    hiddenimports=[*toast_imports, "tzlocal", *collector_modules],
    excludes=[*COMMON_EXCLUDES, *QT_STACK, "reportlab", "bytelog.ui", "bytelog.reports"],
    noarchive=False,
)

dashboard_pyz = PYZ(dashboard.pure)  # noqa: F821
collector_pyz = PYZ(collector.pure)  # noqa: F821

dashboard_exe = EXE(  # noqa: F821
    dashboard_pyz,
    dashboard.scripts,
    [],
    exclude_binaries=True,
    name="ByteLog",
    icon=ICON,
    version=version_info("ByteLog data usage dashboard", "ByteLog"),
    console=False,
    upx=False,
    debug=False,
)

collector_exe = EXE(  # noqa: F821
    collector_pyz,
    collector.scripts,
    [],
    exclude_binaries=True,
    name="ByteLogCollector",
    icon=ICON,
    version=version_info("ByteLog background collector", "ByteLogCollector"),
    console=False,
    upx=False,
    debug=False,
)

coll = COLLECT(  # noqa: F821
    dashboard_exe,
    dashboard.binaries,
    dashboard.datas,
    collector_exe,
    collector.binaries,
    collector.datas,
    strip=False,
    upx=False,
    name="ByteLog",
)
