# setup_schedule.ps1
# Registers 5 Windows Task Scheduler entries that fire at 6:30, 7:30, 8:30,
# 9:30, 10:30 ET every day. Each one runs run_scanner.ps1.
#
# RUN AS ADMINISTRATOR (right-click PowerShell → Run as Administrator)
# then:  .\setup_schedule.ps1
#
# To remove later: .\setup_schedule.ps1 -Remove

param([switch]$Remove)

$projDir = "c:\Users\adamm\Desktop\Coding\stockproj"
$script  = Join-Path $projDir "run_scanner.ps1"
$times   = @("03:30", "04:30", "05:30", "06:30", "07:30")

# IMPORTANT: scheduler runs in LOCAL TIME. If your PC is on Eastern, these times
# are already correct. If not, adjust below to whatever local time corresponds
# to 6:30-10:30 ET (e.g. PT = subtract 3h → 03:30, 04:30, 05:30, 06:30, 07:30).
# Detect local offset from ET and warn if it differs.
$et       = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$local    = [System.TimeZoneInfo]::Local
$nowUtc   = (Get-Date).ToUniversalTime()
$etOffset    = $et.GetUtcOffset($nowUtc).TotalHours
$localOffset = $local.GetUtcOffset($nowUtc).TotalHours
if ($etOffset -ne $localOffset) {
    Write-Host "WARNING: your local timezone is not Eastern." -ForegroundColor Yellow
    Write-Host "  Local offset: UTC$([string]::Format('{0:+#;-#;+0}', $localOffset))" -ForegroundColor Yellow
    Write-Host "  ET    offset: UTC$([string]::Format('{0:+#;-#;+0}', $etOffset))" -ForegroundColor Yellow
    $diff = $etOffset - $localOffset
    Write-Host "  Edit `$times in this script: subtract $diff hour(s) from each entry." -ForegroundColor Yellow
    Write-Host ""
}

foreach ($t in $times) {
    $taskName = "StockScanner_${t}".Replace(":", "")

    if ($Remove) {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Write-Host "Removed $taskName" -ForegroundColor Green
        } catch {
            Write-Host "Not found: $taskName" -ForegroundColor DarkGray
        }
        continue
    }

    $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
                 -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
                 -WorkingDirectory $projDir

    $trigger = New-ScheduledTaskTrigger -Daily -At $t

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                  -DontStopOnIdleEnd -AllowStartIfOnBatteries `
                  -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered $taskName at $t (local)" -ForegroundColor Green
}

if (-not $Remove) {
    Write-Host ""
    Write-Host "Done. View tasks with:" -ForegroundColor Cyan
    Write-Host "  Get-ScheduledTask -TaskName 'StockScanner_*' | Format-Table -AutoSize"
    Write-Host "Run one manually:" -ForegroundColor Cyan
    Write-Host "  Start-ScheduledTask -TaskName 'StockScanner_0630'"
}
