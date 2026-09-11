# Build the Windows executables into dist/.
#
#   .\build_exe.ps1
#
# Produces:
#   dist\QSL卡片打印系统.exe   windowed GUI (double-click to run)
#   dist\qslcard-cli.exe       console CLI (same core, scriptable)
#
# Notes: native tools (PyInstaller, pytest) write warnings to stderr, which
# PowerShell would turn into a terminating error under $ErrorActionPreference
# "Stop".  Exit codes are therefore checked explicitly instead.  The frozen GUI
# self-test is judged by the JSON report it writes, because a windowed process
# launched through Start-Process does not always yield a usable ExitCode.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Output "==> $Name"
    $global:LASTEXITCODE = 0
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit code $LASTEXITCODE)" }
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run: python -m venv .venv, then: .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt"
}

# PyInstaller cannot overwrite an executable that is still running.
$running = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -like "QSL*" -or $_.ProcessName -like "qslcard*" }
if ($running) {
    throw "请先关闭正在运行的程序再构建：$($running.ProcessName -join ', ')"
}

Invoke-Step "generating icon" { & $python scripts\make_icon.py }
Invoke-Step "running tests" { & $python -m pytest -q }
Invoke-Step "building executables" { & $python -m PyInstaller --noconfirm --clean qslcard.spec }

Write-Output "==> result"
Get-ChildItem (Join-Path $root "dist") -File | ForEach-Object {
    "{0,-28} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)
}

Write-Output "==> self-test of the frozen GUI binary (windowed)"
$dist = Join-Path $root "dist"
# Discover the binary instead of embedding its (non-ASCII) name: a literal here
# would depend on the encoding this script file is read with.
$gui = Get-ChildItem $dist -File -Filter "*.exe" |
    Where-Object { $_.BaseName -ne "qslcard-cli" } |
    Sort-Object Length -Descending |
    Select-Object -First 1
if ($null -eq $gui) { throw "GUI executable not found in $dist" }
Write-Output "GUI binary: $($gui.Name)"
$work = Join-Path $env:TEMP ("qsl-selftest-" + (Get-Random))
Write-Output "work dir: $work"
$proc = Start-Process -FilePath $gui.FullName -ArgumentList '--selftest', $work -Wait -PassThru
if ($null -eq $proc) {
    Write-Output "note: Start-Process returned no process object; judging by the report instead"
} else {
    Write-Output "GUI selftest exit code: $($proc.ExitCode)"
}
$report = Join-Path $work "selftest-report.json"
if (-not (Test-Path $report)) {
    throw "frozen GUI self-test wrote no report at $report"
}
$json = Get-Content $report -Raw | ConvertFrom-Json
Get-Content $report -Raw
if (-not $json.ok) { throw "frozen GUI self-test reported failure" }
Write-Output "GUI self-test OK: $($json.cards.cards) cards, $($json.pdf.pages) pages, $($json.pdf.bytes) bytes"

Write-Output "==> self-test of the frozen CLI binary (console)"
$work2 = Join-Path $env:TEMP ("qsl-selftest-cli-" + (Get-Random))
& (Join-Path $root "dist\qslcard-cli.exe") --selftest $work2 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "frozen CLI self-test failed (exit code $LASTEXITCODE)" }
$report2 = Join-Path $work2 "selftest-report.json"
$json2 = Get-Content $report2 -Raw | ConvertFrom-Json
if (-not $json2.ok) { throw "frozen CLI self-test reported failure" }
Write-Output "CLI self-test OK"

Write-Output "both frozen binaries passed the self-test"
