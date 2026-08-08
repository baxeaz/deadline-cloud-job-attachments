# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Produce a copy of python.exe whose embedded manifest does NOT declare longPathAware.

Why: the LongPathsEnabled registry setting only takes effect for processes whose
application manifest declares longPathAware. Stock python.exe declares it (since 3.6),
so a stock interpreter can never reproduce the environment of a DCC-embedded
interpreter or pywin32's pythonservice.exe. This tool clears the flag in a copy,
giving a stand-in "application" for that environment without needing a DCC install.

How: byte-patches the copy, changing the manifest's longPathAware VALUE from 'true'
to 'fals' (same length). The element name is left alone: SxS schema-validates the
element names in the windowsSettings namespaces at activation-context creation and
rejects unknown ones with WinError 14001, but the value text is not validated there.
At runtime the loader compares the value against "true" (case-insensitive); anything
else means the setting is off. Same-length substitution means no PE resource tables,
sizes, or offsets change, and no UpdateResourceW/ctypes-callback machinery is needed
(the resource-API approach fatally crashes some interpreters, e.g. conda/miniforge
Python 3.13).

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
import re
import shutil
import subprocess
import sys

# Matches the opening tag through its value: <ws2:longPathAware ...>true
# The value is replaced with same-length 'fals', which the runtime's exact
# (case-insensitive) comparison against "true" treats as off. The element NAME must
# not be altered: SxS validates windowsSettings element names at activation-context
# creation and an unknown name kills process start with WinError 14001.
_VALUE_PATTERN = re.compile(rb"(longPathAware[^<]{0,200}?>\s*)true", re.IGNORECASE)
_REPLACEMENT_VALUE = b"fals"


def patch_manifest_bytes(exe_path: str) -> int:
    """Flip every longPathAware value from 'true' to 'fals' in the binary.

    Returns the number of occurrences patched. Same-length substitution, so no PE
    offsets change.
    """
    with open(exe_path, "rb") as f:
        blob = f.read()

    patched_blob, count = _VALUE_PATTERN.subn(rb"\g<1>" + _REPLACEMENT_VALUE, blob)
    if count:
        assert len(patched_blob) == len(blob), "patch must not change binary size"
        with open(exe_path, "wb") as f:
            f.write(patched_blob)
    return count


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
            f"ERROR: no 'longPathAware...>true' occurrence found in {dst}, yet the "
            "interpreter reports itself long path aware. Its manifest may live in an "
            "external .manifest file or a launcher; patch that instead.",
            file=sys.stderr,
        )
        os.unlink(dst)
        return 1
    if patched > 2:
        print(
            f"ERROR: {patched} occurrences patched -- more than a manifest should "
            "produce. Refusing to trust the result; inspect the binary.",
            file=sys.stderr,
        )
        os.unlink(dst)
        return 1

    try:
        still_aware = is_long_path_aware(dst)
    except OSError as e:
        print(
            f"ERROR: patched interpreter failed to launch ({e}). The manifest edit "
            "was rejected by the loader; the copy has been removed.",
            file=sys.stderr,
        )
        os.unlink(dst)
        return 1
    if still_aware:
        print(
            "ERROR: patched interpreter still reports RtlAreLongPathsEnabled()=1; "
            "the manifest edit did not take effect.",
            file=sys.stderr,
        )
        os.unlink(dst)
        return 1

    print(f"patched {patched} manifest occurrence(s)", file=sys.stderr)
    print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
