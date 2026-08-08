# Windows long-path repro harness (worker-agent#520 / job-attachments PR #67)

Answers, with measurements instead of theory:

1. Does the theorized #520 error state reproduce — a job-attachments file operation
   failing with `WinError 3` on a >260-char path inside a process that does **not**
   declare `longPathAware` (DCC-embedded interpreter, `pythonservice.exe`), on a host
   with the `LongPathsEnabled` registry key **on**?
2. Does PR #67 fix it?
3. Does the output-sync enumeration path (`asset_sync._get_output_files`), which the
   PR does not touch, fail independently?

No DCC is required: `make_non_longpathaware_python.py` produces a copy of your Python
interpreter with the `longPathAware` manifest flag cleared, which is a faithful
stand-in for the customer's process environment.

## Requirements

- Windows 10/11, Python 3.9+ on PATH, git
- `LongPathsEnabled = 1` in the registry (the scenario under test). Admin PowerShell:
  `Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1`
- No AWS access needed (S3 is moto-mocked); no admin needed beyond the registry check.

## Run

```powershell
git fetch origin pull/67/head:pr-67   # or your fork's branch name
powershell -ExecutionPolicy Bypass -File repro\run_repro.ps1
```

Runs the full matrix {mainline, pr-67} x {stock, non-longPathAware python} and prints
a verdict table. Per-probe exceptions with the exact throwing library frame land in
`C:\lp-repro-work\result-*.json`.

## What the probes drive

| Probe | Real code exercised | Why |
|---|---|---|
| `download` | `download_file()` → real s3transfer `TransferManager` → temp-suffixed file write | The frame #520's error message points at (`...longpath_test.exr.f307214C`) |
| `output-sync` | `AssetSync._get_output_files()` → `glob`/`stat`/`hash_file` over a real long tree | The frame leongdl's review says PR #67 does not cover |

Both probes fail on **silent** misbehavior too (file missing after a "successful"
download; long output dropped from the enumeration), not just on exceptions.

## Interpreting the matrix

| Observation | Meaning |
|---|---|
| `mainline` + non-aware FAILs | Control valid: the harness reproduces a real defect state |
| `mainline` all PASS | The theorized error state does not reproduce; PR #67's "why now" premise is wrong and #520 needs a different root cause |
| `pr-67` download PASS | The PR fixes the download half of #520 |
| `pr-67` output-sync FAIL | The upload half of #520 is NOT fixed; the JSON `library_frame` names exactly where the follow-up fix goes |

## Cleanup

```powershell
git worktree remove C:\lp-repro-work\src-mainline; git worktree remove C:\lp-repro-work\src-pr-67
Remove-Item -Recurse -Force C:\lp-repro-work
# and the patched interpreter next to your python.exe:
Remove-Item (Join-Path (Split-Path (Get-Command python).Source) "python-nolpa.exe")
```
