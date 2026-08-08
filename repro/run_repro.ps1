# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
<#
.SYNOPSIS
Runs the long-path repro matrix: {mainline, pr-67} x {stock python, non-longPathAware python}.

.DESCRIPTION
For each code-under-test branch, installs the package (plus moto/xxhash) into an
isolated --target directory, then runs repro\windows_longpath_repro.py under both a
stock interpreter and a manifest-patched non-longPathAware interpreter (the stand-in
for a DCC-embedded interpreter / pythonservice.exe).

Interpretation:
  mainline + non-aware  : the control. If the theorized #520 state is real, this FAILS.
                          If it PASSES, the PR's motivating story is wrong (the process-
                          scoped RtlAreLongPathsEnabled already applied the prefix).
  mainline + stock      : crowecawcaw's probe predicts download FAILS here (the registry
                          gate suppressed the prefix; stock python survives via its own
                          awareness EXCEPT where the code builds paths that awareness
                          cannot rescue).
  pr-67 + both          : the verdict. download must PASS. If output-sync FAILS, the fix
                          is incomplete for #520's upload half and we know the follow-up.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File repro\run_repro.ps1
#>
[CmdletBinding()]
param(
    # Refs to test. pr-67 exists after: git fetch origin pull/67/head:pr-67 (or use your
    # fork's branch name).
    [string[]]$Refs = @("mainline", "pr-67"),
    # Working directory for worktrees, installed deps, and results.
    [string]$WorkDir = "C:\lp-repro-work"
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$reproDir = Join-Path $repoRoot "repro"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# --- Preflight: registry must be ON (the scenario under test) -----------------------
$reg = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue
if (-not $reg -or $reg.LongPathsEnabled -ne 1) {
    Write-Error ("LongPathsEnabled registry value is not 1. The scenario under test is " +
        "'registry ON'. Enable it (admin PowerShell):`n" +
        '  Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1' +
        "`nthen re-run.")
}

# --- Locate a stock python -----------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction Stop).Source }
Write-Host "Stock python: $python"

# --- Build the non-longPathAware interpreter -----------------------------------------
Write-Host "`n=== Building non-longPathAware python ==="
$nolpa = & $python (Join-Path $reproDir "make_non_longpathaware_python.py") 2>&1
if ($LASTEXITCODE -ne 0) {
    # Install dir not writable (e.g. system-wide install): fall back to a tree copy.
    Write-Host "Same-directory patch failed; retrying with --copy-tree"
    $nolpa = & $python (Join-Path $reproDir "make_non_longpathaware_python.py") --copy-tree (Join-Path $WorkDir "py-nolpa") 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not build non-longPathAware python:`n$nolpa" }
}
$nolpa = ($nolpa | Select-Object -Last 1).ToString().Trim()
Write-Host "Non-aware python: $nolpa"

# --- Run the matrix -------------------------------------------------------------------
$matrix = @()
foreach ($ref in $Refs) {
    $safeRef = $ref -replace '[/\\]', '-'
    $worktree = Join-Path $WorkDir "src-$safeRef"
    $deps = Join-Path $WorkDir "deps-$safeRef"

    if (-not (Test-Path $worktree)) {
        Write-Host "`n=== Preparing worktree for $ref ==="
        git -C $repoRoot worktree add --detach $worktree $ref
        if ($LASTEXITCODE -ne 0) { Write-Error "git worktree add failed for $ref" }
    }
    if (-not (Test-Path $deps)) {
        Write-Host "=== Installing $ref into $deps ==="
        & $python -m pip install --quiet --target $deps $worktree moto xxhash
        if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed for $ref" }
    }

    foreach ($mode in @("stock", "non-aware")) {
        $exe = if ($mode -eq "stock") { $python } else { $nolpa }
        $flags = if ($mode -eq "non-aware") { @("--require-host-unaware") } else { @() }
        $jsonOut = Join-Path $WorkDir "result-$safeRef-$mode.json"

        Write-Host "`n=== $ref / $mode python ===" -ForegroundColor Cyan
        $env:PYTHONPATH = $deps
        # -s: ignore user site-packages so only $deps supplies the package under test.
        & $exe -s (Join-Path $reproDir "windows_longpath_repro.py") @flags --json $jsonOut
        $code = $LASTEXITCODE
        $env:PYTHONPATH = $null

        $probes = if (Test-Path $jsonOut) { Get-Content $jsonOut | ConvertFrom-Json } else { @() }
        $matrix += [pscustomobject]@{
            Ref        = $ref
            Python     = $mode
            ExitCode   = $code
            Download   = ($probes | Where-Object probe -eq "download").result
            OutputSync = ($probes | Where-Object probe -eq "output-sync").result
        }
    }
}

# --- Verdict --------------------------------------------------------------------------
Write-Host "`n============================ MATRIX ============================" -ForegroundColor Yellow
$matrix | Format-Table -AutoSize
Write-Host @"
How to read this:
 * mainline row(s) with FAIL  -> the harness reproduces a real defect (control is valid).
 * mainline all PASS          -> the theorized error state does not reproduce; the PR's
                                 'why now' premise needs re-examination before trusting it.
 * pr-67 download PASS        -> the PR fixes the frame #520's error message points at.
 * pr-67 output-sync FAIL     -> #520's upload half is NOT fixed by this PR; the
                                 library_frame field in result-*.json names the follow-up.
Per-probe exceptions and throwing frames: $WorkDir\result-*.json
"@
