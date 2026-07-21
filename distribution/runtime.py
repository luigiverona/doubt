"""Build the deterministic self-contained Arch x86_64 runtime directory."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from doubt.core.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_NAME = f"doubt-{VERSION}-x86_64"
LICENSE_SOURCES = {
    "CPython.txt": Path("/usr/lib/python3.14/LICENSE.txt"),
    "OpenSSL.txt": Path("/usr/share/licenses/openssl/LICENSE.txt"),
    "bzip2.txt": Path("/usr/share/licenses/bzip2/LICENSE"),
    "Brotli.txt": Path("/usr/share/licenses/brotli/LICENSE"),
    "mpdecimal.txt": Path("/usr/share/licenses/mpdecimal/COPYRIGHT.txt"),
    "XZ-COPYING.txt": ROOT / "release/licenses/xz/COPYING",
    "XZ-0BSD.txt": ROOT / "release/licenses/xz/COPYING.0BSD",
    "XZ-GPLv2.txt": ROOT / "release/licenses/xz/COPYING.GPLv2",
    "zlib.txt": Path("/usr/share/licenses/zlib/LICENSE"),
    "Zstandard.txt": Path("/usr/share/licenses/zstd/LICENSE"),
}


class RuntimeError(ValueError):
    """Invalid or nondeterministic runtime build state."""


def build(output: Path, work: Path) -> Path:
    _validate_environment()
    output = output.resolve()
    work = work.resolve()
    if output.exists() or work.exists():
        raise RuntimeError("runtime output and work paths must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONHASHSEED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(work / "pycache"),
        "SOURCE_DATE_EPOCH": "0",
        "PYINSTALLER_CONFIG_DIR": str(work / "config"),
        "XDG_CACHE_HOME": str(work / "cache"),
    }
    for variable in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(variable, None)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--name",
        "doubt",
        "--contents-directory",
        "_internal",
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work / "spec"),
        "--paths",
        str(ROOT),
        "--add-data",
        f"{ROOT / 'apps'}:apps",
        "--add-data",
        f"{ROOT / 'deps'}:deps",
        str(ROOT / "doubt/entry.py"),
    ]
    subprocess.run(command, check=True, env=env, cwd=ROOT)
    built = work / "dist/doubt"
    shutil.copytree(built, output)
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    shutil.copy2(ROOT / "release/components.json", output / "COMPONENTS.json")
    licenses = output / "licenses"
    licenses.mkdir(mode=0o755)
    for name, source in LICENSE_SOURCES.items():
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"required runtime license is unavailable: {source}")
        shutil.copy2(source, licenses / name)
    try:
        distribution = metadata.distribution("pyinstaller")
    except metadata.PackageNotFoundError as error:
        raise RuntimeError("PyInstaller is unavailable") from error
    relative_license = next(
        (item for item in (distribution.files or ()) if item.as_posix().endswith("/licenses/COPYING.txt")),
        None,
    )
    pyinstaller_license = (
        Path(str(distribution.locate_file(relative_license))) if relative_license else Path()
    )
    if not pyinstaller_license.is_file():
        raise RuntimeError("PyInstaller bootloader license is unavailable")
    shutil.copy2(pyinstaller_license, licenses / "PyInstaller.txt")
    validate(output)
    return output


def validate(path: Path) -> None:
    if path.name != BUNDLE_NAME or path.is_symlink() or not path.is_dir():
        raise RuntimeError("runtime bundle root differs")
    expected_top = {"doubt", "_internal", "LICENSE", "COMPONENTS.json", "licenses"}
    if {item.name for item in path.iterdir()} != expected_top:
        raise RuntimeError("runtime bundle top-level members differ")
    components = json.loads((path / "COMPONENTS.json").read_text(encoding="utf-8"))
    if components.get("architecture") != "x86_64":
        raise RuntimeError("runtime component architecture differs")
    for item in (path, *path.rglob("*")):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"runtime symbolic links are forbidden: {item.relative_to(path)}")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise RuntimeError(f"unsupported runtime member: {item.relative_to(path)}")
    executable = path / "doubt"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("runtime executable is unavailable")


def _validate_environment() -> None:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("runtime builds require Linux x86_64")
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if version != "3.14.6":
        raise RuntimeError(f"runtime builds require CPython 3.14.6, found {version}")
    components = json.loads((ROOT / "release/components.json").read_text(encoding="utf-8"))
    versions = {item["name"]: item["version"] for item in components["components"]}
    if versions.get("doubt") != VERSION or versions.get("CPython") != version:
        raise RuntimeError("runtime component versions differ from source")
