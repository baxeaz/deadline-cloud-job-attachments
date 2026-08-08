# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Produce a copy of python.exe whose embedded manifest does NOT declare longPathAware.

Why: the LongPathsEnabled registry setting only takes effect for processes whose
application manifest declares longPathAware. Stock python.exe declares it (since 3.6),
so a stock interpreter can never reproduce the environment of a DCC-embedded
interpreter or pywin32's pythonservice.exe. This tool clears the flag in a copy,
giving a stand-in "application" for that environment without needing a DCC install.

How: byte-patches the copy, renaming the manifest's `longPathAware` element to a
same-length unknown name. The Windows loader ignores unknown windowsSettings
elements, so the effect is identical to never declaring the setting. Same-length
substitution means no PE resource tables, sizes, or offsets change, and no
UpdateResourceW/ctypes-callback machinery is needed (the resource-API approach
fatally crashes some interpreters, e.g. conda/miniforge Python 3.13).

The copy is placed in the SAME directory as the source python.exe so it finds
python3xx.dll and the standard library without further setup. If that directory is
not writable, pass --copy-tree DIR to copy the install and patch there instead.

Usage (Windows only):
    python make_non_longpathaware_python.py [--name python-nolpa.exe] [--copy-tree DIR]

Prints the path of the patched interpreter on success, after verifying that
RtlAreLongPathsEnabled() returns False inside it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

NEEDLE = b"longPathAware"
# Same length, unknown element name: <ws2:XongPathAware>true</...> is ignored by the
# loader, which is equivalent to not declaring the setting at all.
REPLACEMENT = b"XongPathAware"
assert len(NEEDLE) == len(REPLACEMENT)


def patch_manifest_bytes(exe_path: str) -> int:
    """Rename every tag-context occurrence of longPathAware in the binary.

    Returns the number of occurrences patched. Only occurrences immediately preceded
    by ':', '<', or '/' (i.e. XML tag names like <ws2:longPathAware> and
    </ws2:longPathAware>) are touched, so an incidental occurrence of the string in
    code or data is left alone.
    """
    with open(exe_path, "rb") as f:
        blob = bytearray(f.read())

    patched = 0
    start = 0
    while True:
        i = blob.find(NEEDLE, start)
        if i == -1:
            break
        if i > 0 and blob[i - 1 : i] in (b":", b"<", b"/"):
            blob[i : i + len(NEEDLE)] = REPLACEMENT
            patched += 1
        start = i + len(NEEDLE)

    if patched:
        with open(exe_path, "wb") as f:
            f.write(blob)
    return patched


def is_long_path_aware(exe: str) -> bool:
    """Return RtlAreLongPathsEnabled() as evaluated INSIDE the given interpreter."""
    out = subprocess.run(
        [
            exe,
            "-c",
            (
                "import ctypes;"
                "n=ctypes.WinDLL('ntdll');"
                "n.RtlAreLongPathsEnabled.restype=ctypes.c_ubyte;"
                "print(int(n.RtlAreLongPathsEnabled()))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip() == "1"


def registry_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def main() -> int:
    if sys.platform != "win32":
        print("ERROR: this tool only runs on Windows.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="python-nolpa.exe", help="Name of the patched copy")
    parser.add_argument(
        "--copy-tree",
        metavar="DIR",
        default=None,
        help=(
            "Copy the entire python install directory to DIR and patch the copy there. "
            "Use when the python install directory is not writable. NOTE: can be large "
            "for conda installs."
        ),
    )
    args = parser.parse_args()

    if not registry_enabled():
        print(
            "ERROR: LongPathsEnabled registry value is not 1. With the registry off, "
            "RtlAreLongPathsEnabled() is 0 for every process and this tool cannot "
            "verify its own work (nor is there a scenario to reproduce). Enable it "
            "and re-run.",
            file=sys.stderr,
        )
        return 2

    # A venv python.exe is a launcher; patch the base interpreter instead so the
    # manifest we clear is the one the real process runs under.
    base = getattr(sys, "_base_executable", None) or sys.executable

    if not is_long_path_aware(base):
        # Nothing to patch: this interpreter already behaves like the customer's
        # process. Copy it under the expected name so callers get a distinct exe.
        print(
            f"NOTE: {base} is ALREADY not long path aware (its manifest does not "
            "declare longPathAware, or declares it false). Using a plain copy. "
            "Beware: your 'stock' interpreter is then not an aware control -- use a "
            "python.org build for the aware side of the matrix.",
            file=sys.stderr,
        )
        dst = os.path.join(os.path.dirname(base), args.name)
        shutil.copy2(base, dst)
        print(dst)
        return 0

    if args.copy_tree:
        base_dir = os.path.dirname(base)
        dst_dir = os.path.abspath(args.copy_tree)
        if not os.path.isdir(dst_dir):
            shutil.copytree(base_dir, dst_dir)
        dst = os.path.join(dst_dir, args.name)
        shutil.copy2(os.path.join(dst_dir, os.path.basename(base)), dst)
    else:
        dst = os.path.join(os.path.dirname(base), args.name)
        try:
            shutil.copy2(base, dst)
        except PermissionError:
            print(
                f"ERROR: {os.path.dirname(base)} is not writable. Re-run with "
                f"--copy-tree C:\\path\\to\\writable\\dir",
                file=sys.stderr,
            )
            return 2

    patched = patch_manifest_bytes(dst)
    if patched == 0:
        print(
            f"ERROR: no tag-context 'longPathAware' occurrences found in {dst}, yet the "
            "interpreter reports itself long path aware. Its manifest may live in an "
            "external .manifest file or a launcher; patch that instead.",
            file=sys.stderr,
        )
        return 1
    if patched > 4:
        print(
            f"ERROR: {patched} occurrences patched -- more than a manifest open+close "
            "tag pair should produce. Refusing to trust the result; inspect the binary.",
            file=sys.stderr,
        )
        os.unlink(dst)
        return 1

    if is_long_path_aware(dst):
        print(
            "ERROR: patched interpreter still reports RtlAreLongPathsEnabled()=1; "
            "the manifest edit did not take effect.",
            file=sys.stderr,
        )
        return 1

    print(f"patched {patched} manifest occurrence(s)", file=sys.stderr)
    print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
