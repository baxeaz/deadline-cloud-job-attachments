# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""End-to-end Windows long-path repro for deadline-cloud-worker-agent#520.

Drives the REAL job-attachments code paths (not the path-helper primitives) with
long (>260 char) paths, and reports which library frame throws. Run it against
mainline and against the PR branch, under a stock python and under a
non-longPathAware python (see make_non_longpathaware_python.py), to answer:

  1. Does the reported failure (WinError 3 during a temp-suffixed download write)
     reproduce at all, and under which process type?
  2. Does the PR fix it?
  3. Does output-sync enumeration (asset_sync._get_output_files: glob/stat/hash),
     which the PR does NOT touch, fail independently?

The probe imports only the installed `deadline.job_attachments` package, so the
same script runs unchanged against any branch: select the code under test with
PYTHONPATH (see run_repro.ps1).

Probes:
  download      real download_file() through the real s3transfer TransferManager
                against a moto-mocked S3 bucket. This is the frame worker-agent#520's
                error message points at (temp-download suffix, download.py).
  output-sync   real AssetSync._get_output_files() over a real on-disk output tree
                whose paths exceed 260 chars. This is the frame leongdl's review
                claims is uncovered by the PR.

Exit code: 0 if all probes pass, 1 if any fail (a control run on mainline is
EXPECTED to exit 1 -- that is what validates the harness).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from threading import Lock

WIN = sys.platform == "win32"
# Fixture paths are always created through the extended-length form so that fixture
# setup itself never trips the defect under test.
FIXTURE_PREFIX = "\\\\?\\" if WIN else ""

results: list[dict] = []


def _throwing_frames(exc: BaseException) -> tuple[str, str]:
    """(deepest frame inside deadline.job_attachments, deepest frame overall)."""
    frames = traceback.extract_tb(exc.__traceback__)
    lib_frame = ""
    for f in frames:
        norm = f.filename.replace("/", os.sep).replace("\\", os.sep)
        if os.sep + os.path.join("deadline", "job_attachments") + os.sep in norm:
            lib_frame = f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}"
    last = frames[-1] if frames else None
    last_frame = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}" if last else ""
    return lib_frame, last_frame


def record(name: str, ok: bool, detail: str, exc: BaseException | None = None) -> None:
    entry = {"probe": name, "result": "PASS" if ok else "FAIL", "detail": detail}
    if exc is not None:
        lib_frame, last_frame = _throwing_frames(exc)
        entry["exception"] = f"{type(exc).__name__}: {exc}"
        entry["library_frame"] = lib_frame
        entry["deepest_frame"] = last_frame
    results.append(entry)
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")
    if exc is not None:
        print(f"       exception:     {entry['exception']}")
        print(f"       library frame: {entry.get('library_frame') or '(none)'}")
        print(f"       deepest frame: {entry.get('deepest_frame')}")


def environment_report(require_host_unaware: bool) -> bool:
    """Print the process/host long-path state; enforce --require-host-unaware."""
    print(f"python:      {sys.version.split()[0]}  ({sys.executable})")
    if not WIN:
        print("platform:    NOT WINDOWS -- this probe must run on Windows.")
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            registry_value = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    except OSError:
        registry_value = 0

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
    process_aware = bool(ntdll.RtlAreLongPathsEnabled())

    print(f"registry:    HKLM ...\\FileSystem\\LongPathsEnabled = {registry_value}")
    print(f"process:     RtlAreLongPathsEnabled() = {process_aware}")
    print(
        "scenario:    "
        + (
            "registry ON, process NOT longPathAware  <-- the #520 customer configuration"
            if registry_value == 1 and not process_aware
            else "registry ON, process longPathAware (stock python; prefix not strictly needed)"
            if registry_value == 1 and process_aware
            else "registry OFF (prefix needed by every process)"
        )
    )

    if registry_value != 1:
        print(
            "ERROR: LongPathsEnabled registry value is not 1. The scenario under test is\n"
            "registry ON. Enable it and re-run:\n"
            '  Set-ItemProperty "HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem" '
            '-Name LongPathsEnabled -Value 1  (then reboot or re-login)'
        )
        return False
    if require_host_unaware and process_aware:
        print(
            "ERROR: --require-host-unaware was given but this process IS long path aware.\n"
            "You are running under stock python; use the interpreter produced by\n"
            "make_non_longpathaware_python.py. Refusing to continue so a misconfigured\n"
            "run cannot masquerade as a passing one."
        )
        return False
    return True


def make_long_relative_path(base_dir: str, target_total: int = 320) -> str:
    """A relative path (forward slashes, as manifests use) that pushes
    base_dir/<rel> beyond target_total characters, crossing 260 at a directory level."""
    rel_parts = []
    running = len(base_dir)
    i = 0
    while running < target_total:
        part = f"component_{i:02d}_" + "x" * 20
        rel_parts.append(part)
        running += len(part) + 1
        i += 1
    rel_parts.append("longpath_test_output.exr")
    return "/".join(rel_parts)


def probe_download(tmp_root: str) -> None:
    """Real download_file() through the real TransferManager against moto S3.

    The temp-suffixed write inside s3transfer is the exact frame worker-agent#520's
    error message points at ('...longpath_test.exr.f307214C')."""
    import boto3
    from moto import mock_aws

    from deadline.job_attachments.asset_manifests.hash_algorithms import HashAlgorithm
    from deadline.job_attachments.asset_manifests.v2023_03_03.asset_manifest import ManifestPath
    from deadline.job_attachments.download import download_file

    download_dir = os.path.join(tmp_root, "dl")
    os.makedirs(FIXTURE_PREFIX + download_dir, exist_ok=True)
    rel_path = make_long_relative_path(download_dir)
    final_path = os.path.join(download_dir, rel_path.replace("/", os.sep))
    print(f"       download target length: {len(final_path)} chars")

    data = b"deadline long path repro payload"
    import xxhash

    file_hash = xxhash.xxh3_128(data).hexdigest()

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="longpath-repro")
        s3.put_object(
            Bucket="longpath-repro", Key=f"Data/{file_hash}.xxh128", Body=data
        )
        manifest_file = ManifestPath(
            path=rel_path, hash=file_hash, size=len(data), mtime=int(time.time() * 1_000_000)
        )
        try:
            file_bytes, out_path = download_file(
                manifest_file,
                HashAlgorithm.XXH128,
                download_dir,
                Lock(),
                defaultdict(int),
                "longpath-repro",
                "Data",
                s3_client=s3,
            )
        except BaseException as exc:  # noqa: BLE001 -- report, don't presume
            record(
                "download",
                False,
                "download_file raised on a >260-char destination",
                exc,
            )
            return

    # Verify the bytes actually landed (via the extended-length form so verification
    # itself cannot fail on the path length).
    on_disk = Path(FIXTURE_PREFIX + final_path)
    if on_disk.is_file() and on_disk.read_bytes() == data:
        record("download", True, f"file downloaded and verified at {len(final_path)} chars")
    else:
        record(
            "download",
            False,
            f"download_file returned {out_path!r} without raising, but the file is missing "
            "or wrong at the expected destination -- a silent-failure mode",
        )


def probe_output_sync(tmp_root: str) -> None:
    """Real AssetSync._get_output_files() over a real long output tree.

    This is the output-sync enumeration path (glob/stat/hash) that the PR does not
    touch. A failure here with the PR applied means #520's upload half is still open."""
    import boto3
    from moto import mock_aws

    from deadline.job_attachments.asset_sync import AssetSync
    from deadline.job_attachments.models import (
        JobAttachmentS3Settings,
        ManifestProperties,
        PathFormat,
    )

    session_dir = os.path.join(tmp_root, "session")
    local_root = os.path.join(session_dir, "assetroot")
    rel_file = make_long_relative_path(local_root)
    abs_file = os.path.join(local_root, "output", rel_file.replace("/", os.sep))
    print(f"       output file length: {len(abs_file)} chars")

    os.makedirs(FIXTURE_PREFIX + os.path.dirname(abs_file), exist_ok=True)
    with open(FIXTURE_PREFIX + abs_file, "wb") as f:
        f.write(b"rendered output bytes")

    with mock_aws():
        asset_sync = AssetSync(farm_id="farm-longpathrepro", boto3_session=boto3.Session())
        # S3 head-object is not under test; force the "not yet uploaded" answer.
        asset_sync.s3_uploader.file_already_uploaded = lambda *a, **k: False  # type: ignore[method-assign]

        manifest_properties = ManifestProperties(
            rootPath=local_root,
            rootPathFormat=PathFormat.get_host_path_format(),
            outputRelativeDirectories=["output"],
        )
        s3_settings = JobAttachmentS3Settings(
            s3BucketName="longpath-repro", rootPrefix="rootPrefix"
        )
        try:
            output_files = asset_sync._get_output_files(
                manifest_properties, s3_settings, Path(local_root), Path(session_dir)
            )
        except BaseException as exc:  # noqa: BLE001
            record(
                "output-sync",
                False,
                "_get_output_files raised while enumerating a >260-char output tree",
                exc,
            )
            return

    if len(output_files) != 1:
        # Files silently skipped (e.g. glob or the containment check dropping them)
        # never upload: from the customer's perspective, outputs vanish. That is a
        # failure even though nothing raised.
        record(
            "output-sync",
            False,
            f"expected 1 output file, got {len(output_files)} -- long output was "
            "silently dropped (it would never be uploaded)",
        )
        return

    # The manifest generation step stats each output's full_path again; it is a
    # separate frame with the same long-path exposure.
    try:
        manifest = asset_sync._generate_output_manifest(output_files)
    except BaseException as exc:  # noqa: BLE001
        record(
            "output-sync",
            False,
            "_generate_output_manifest raised while stat-ing a >260-char output path",
            exc,
        )
        return
    if len(manifest.paths) == 1:
        record("output-sync", True, "long output enumerated, hashed, and manifested")
    else:
        record(
            "output-sync",
            False,
            f"expected 1 manifest path, got {len(manifest.paths)}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-host-unaware",
        action="store_true",
        help=(
            "Fail unless this process is NOT long path aware (i.e. running under the "
            "interpreter produced by make_non_longpathaware_python.py). Prevents a "
            "silent fallback to stock python from masquerading as a passing run."
        ),
    )
    parser.add_argument(
        "--json", metavar="FILE", default=None, help="Also write results as JSON to FILE"
    )
    args = parser.parse_args()

    print("=" * 72)
    if not environment_report(args.require_host_unaware):
        return 2
    try:
        import deadline.job_attachments as ja

        print(f"under test:  {os.path.dirname(ja.__file__)}")
    except ImportError as e:
        print(f"ERROR: cannot import deadline.job_attachments: {e}")
        return 2
    print("=" * 72)

    # Keep the base short so only the fixture's own depth crosses 260.
    tmp_root = os.path.join(os.environ.get("REPRO_TMP", "C:\\lp-repro"), str(os.getpid()))
    os.makedirs(tmp_root, exist_ok=True)

    try:
        probe_download(os.path.join(tmp_root, "a"))
        probe_output_sync(os.path.join(tmp_root, "b"))
    finally:
        # Best-effort cleanup through the extended-length form.
        import shutil

        shutil.rmtree(FIXTURE_PREFIX + tmp_root, ignore_errors=True)

    print("=" * 72)
    failed = [r for r in results if r["result"] == "FAIL"]
    for r in results:
        print(f"  {r['result']}  {r['probe']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
