# run_scanner.ps1 — invoked by Windows Task Scheduler
# 6:30 / 7:30 / 8:30 / 9:30 ET  →  --morning  (Strategies 1, 2, 3)
# 10:30 ET                       →  --ten-am   (Strategy 7 + morning set)
#
# Behavior:
#   1. cd into the project, activate the venv
#   2. Run nasdaq_v3.py with --export so each strategy dumps a timestamped CSV
#   3. Move those CSVs into runs/ so the ranker can find them
#   4. Append the full stdout to ScannerRun.txt with a timestamp header

$ErrorActionPreference = "Continue"
$projDir = "c:\Users\adamm\Desktop\Coding\stockproj"
Set-Location $projDir

# Pick the correct flag based on current ET hour
$et       = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$nowEt    = [System.TimeZoneInfo]::ConvertTimeFromUtc((Get-Date).ToUniversalTime(), $et)
$hour     = $nowEt.Hour
$flag     = if ($hour -ge 10) { "--ten-am" } else { "--morning" }
$stamp    = $nowEt.ToString("yyyy-MM-dd HH:mm 'ET'")

$python   = Join-Path $projDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# Capture output
$tmpOut   = Join-Path $projDir "runs\_last_run.txt"
& $python "nasdaq_v3.py" $flag "--export" *>&1 | Tee-Object -FilePath $tmpOut | Out-Null

# Move any newly-produced strategy CSVs into runs\
Get-ChildItem -Path $projDir -Filter "s*_*.csv" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^s\d+_[^_]+_\d{8}_\d{4}\.csv$' } |
    ForEach-Object { Move-Item -Path $_.FullName -Destination (Join-Path $projDir "runs") -Force }

# Append captured stdout to ScannerRun.txt with a clear header
$header   = "`n`n========================================================================`n" +
            "  SCAN @ $stamp   (flag: $flag)`n" +
            "========================================================================`n"
Add-Content -Path (Join-Path $projDir "ScannerRun.txt") -Value $header -Encoding utf8
if (Test-Path $tmpOut) {
    Get-Content $tmpOut | Add-Content -Path (Join-Path $projDir "ScannerRun.txt") -Encoding utf8
    Remove-Item $tmpOut -Force
}
