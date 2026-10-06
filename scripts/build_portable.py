"""Build a Windows PyInstaller onedir bundle and portable ZIP.

Run with the project environment: `python scripts/build_portable.py`.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent.parent
PORTABLE_ROOT = ROOT / "build" / "portable"
WORK_ROOT = ROOT / "build" / "pyinstaller-work"
SPEC_ROOT = ROOT / "build" / "pyinstaller-spec"
BUNDLE = PORTABLE_ROOT / "MessageFilteringAgent"
STATIC_DIR = ROOT / "message_filtering_agent" / "static"
ENTRY_POINT = Path(__file__).resolve().parent / "portable_entry.py"
ARCHIVE = ROOT / "dist" / "MessageFilteringAgent-portable.zip"


def main() -> int:
    """Build the bundle using this interpreter and archive it without dependencies."""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "MessageFilteringAgent",
        "--distpath",
        str(PORTABLE_ROOT),
        "--workpath",
        str(WORK_ROOT),
        "--specpath",
        str(SPEC_ROOT),
        "--add-data",
        f"{STATIC_DIR};message_filtering_agent/static",
        "--collect-all",
        "keyring",
        "--collect-all",
        "langchain_openai",
        "--collect-all",
        "langgraph",
        "--collect-all",
        "langchain_core",
        "--collect-all",
        "pydantic",
        str(ENTRY_POINT),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    executable = BUNDLE / "MessageFilteringAgent.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"PyInstaller did not create {executable}")

    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(ARCHIVE, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in BUNDLE.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(PORTABLE_ROOT))
    print(f"Portable bundle: {BUNDLE}")
    print(f"Portable ZIP: {ARCHIVE} ({ARCHIVE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())