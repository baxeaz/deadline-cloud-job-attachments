# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Produce a copy of python.exe whose embedded manifest does NOT declare longPathAware.

Why: the LongPathsEnabled registry setting only takes effect for processes whose
application manifest declares longPathAware. Stock python.exe declares it (since 3.6),
so a stock interpreter can never reproduce the environment of a DCC-embedded
interpreter or pywin32's pythonservice.exe. This tool clears the flag in a copy,
giving a stand-in "application" for that environment without needing a DCC install.

The copy is placed in the SAME directory as the source python.exe so it finds
python3xx.dll and the standard library without any further setup. If that directory
is not writable, pass --copy-tree to copy the whole install to a writable location
and patch the copy instead.

Usage (Windows only):
    python make_non_longpathaware_python.py [--name python-nolpa.exe] [--copy-tree DIR]

Prints the path of the patched interpreter on success and verifies that
RtlAreLongPathsEnabled() returns False inside it.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import re
import shutil
import subprocess
import sys

RT_MANIFEST = 24
LOAD_LIBRARY_AS_DATAFILE = 0x2
LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x20


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.LoadLibraryExW.restype = wt.HMODULE
    k.LoadLibraryExW.argtypes = (wt.LPCWSTR, wt.HANDLE, wt.DWORD)
    k.FindResourceW.restype = wt.HRSRC
    k.FindResourceW.argtypes = (wt.HMODULE, wt.LPCWSTR, wt.LPCWSTR)
    k.SizeofResource.restype = wt.DWORD
    k.SizeofResource.argtypes = (wt.HMODULE, wt.HRSRC)
    k.LoadResource.restype = wt.HGLOBAL
    k.LoadResource.argtypes = (wt.HMODULE, wt.HRSRC)
    k.LockResource.restype = wt.LPVOID
    k.LockResource.argtypes = (wt.HGLOBAL,)
    k.FreeLibrary.argtypes = (wt.HMODULE,)
    k.BeginUpdateResourceW.restype = wt.HANDLE
    k.BeginUpdateResourceW.argtypes = (wt.LPCWSTR, wt.BOOL)
    k.UpdateResourceW.restype = wt.BOOL
    k.UpdateResourceW.argtypes = (wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR, wt.WORD, wt.LPVOID, wt.DWORD)
    k.EndUpdateResourceW.restype = wt.BOOL
    k.EndUpdateResourceW.argtypes = (wt.HANDLE, wt.BOOL)
    return k


def _intres(i: int):
    """MAKEINTRESOURCE: an integer resource id smuggled through an LPCWSTR argument."""
    return ctypes.cast(ctypes.c_void_p(i), wt.LPCWSTR)


def read_manifest(exe: str) -> tuple[bytes, int]:
    """Return (manifest_bytes, langid) of the exe's RT_MANIFEST resource id 1."""
    k = _kernel32()
    hmod = k.LoadLibraryExW(exe, None, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE)
    if not hmod:
        raise OSError(ctypes.get_last_error(), f"LoadLibraryExW failed for {exe}")
    try:
        # Discover the language id of the manifest rather than assuming 1033.
        langid = None
        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, wt.LPCWSTR, wt.LPCWSTR, wt.WORD, wt.LPARAM)
        def _enum_langs(h, rtype, name, lang, param):
            found.append(lang)
            return True

        k.EnumResourceLanguagesW.argtypes = (
            wt.HMODULE,
            wt.LPCWSTR,
            wt.LPCWSTR,
            ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, wt.LPCWSTR, wt.LPCWSTR, wt.WORD, wt.LPARAM),
            wt.LPARAM,
        )
        k.EnumResourceLanguagesW(hmod, _intres(RT_MANIFEST), _intres(1), _enum_langs, 0)
        if not found:
            raise RuntimeError(f"{exe} has no RT_MANIFEST resource with id 1")
        langid = found[0]

        hres = k.FindResourceW(hmod, _intres(1), _intres(RT_MANIFEST))
        if not hres:
            raise OSError(ctypes.get_last_error(), "FindResourceW failed")
        size = k.SizeofResource(hmod, hres)
        hglob = k.LoadResource(hmod, hres)
        ptr = k.LockResource(hglob)
        return ctypes.string_at(ptr, size), langid
    finally:
        k.FreeLibrary(hmod)


def write_manifest(exe: str, manifest: bytes, langid: int) -> None:
    k = _kernel32()
    h = k.BeginUpdateResourceW(exe, False)
    if not h:
        raise OSError(ctypes.get_last_error(), f"BeginUpdateResourceW failed for {exe}")
    ok = k.UpdateResourceW(h, _intres(RT_MANIFEST), _intres(1), langid, manifest, len(manifest))
    if not ok:
        k.EndUpdateResourceW(h, True)
        raise OSError(ctypes.get_last_error(), "UpdateResourceW failed")
    if not k.EndUpdateResourceW(h, False):
        raise OSError(ctypes.get_last_error(), "EndUpdateResourceW failed")


def clear_long_path_aware(manifest: bytes) -> bytes:
    text = manifest.decode("utf-8")
    # <ws2:longPathAware>true</ws2:longPathAware> (namespace prefix varies)
    patched, n = re.subn(
        r"(<(?:\w+:)?longPathAware>\s*)true(\s*</(?:\w+:)?longPathAware>)",
        r"\g<1>false\g<2>",
        text,
        flags=re.IGNORECASE,
    )
    if n == 0:
        raise RuntimeError(
            "Manifest contains no longPathAware=true element; nothing to clear. "
            "Manifest was:\n" + text
        )
    return patched.encode("utf-8")


def verify(exe: str) -> bool:
    """Return True iff RtlAreLongPathsEnabled() is False inside the given interpreter."""
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
    return out.stdout.strip() == "0"


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
            "Use when the python install directory is not writable."
        ),
    )
    args = parser.parse_args()

    src = sys.executable
    # A venv python.exe is a launcher; patch the base interpreter instead so the
    # manifest we clear is the one the real process runs under.
    base = getattr(sys, "_base_executable", None) or src
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

    manifest, langid = read_manifest(dst)
    write_manifest(dst, clear_long_path_aware(manifest), langid)

    if not verify(dst):
        print(
            "ERROR: patched interpreter still reports RtlAreLongPathsEnabled()=1. "
            "Either the patch did not take, or the LongPathsEnabled registry value is 0 "
            "(in which case there is nothing to distinguish).",
            file=sys.stderr,
        )
        return 1

    print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
