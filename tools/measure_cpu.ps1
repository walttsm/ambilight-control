# Lesson 7b: measure the real CPU cost of a running program.
# Usage:  .\tools\measure_cpu.ps1 -Match 'ambilight.main' -Seconds 20
#         .\tools\measure_cpu.ps1 -Match 'SyncLight' -Seconds 20
param([string]$Match = 'ambilight.main', [int]$Seconds = 20)

function Get-Targets {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match [regex]::Escape($Match) -and $_.Name -ne 'powershell.exe' }
}
function Get-CpuSum($ids) { ($ids | ForEach-Object { (Get-Process -Id $_ -ErrorAction SilentlyContinue).CPU } | Measure-Object -Sum).Sum }

$ids = (Get-Targets).ProcessId
if (-not $ids) { Write-Output "no process matching '$Match'"; exit 1 }
$a = Get-CpuSum $ids
Start-Sleep $Seconds
$b = Get-CpuSum $ids
$ram = ($ids | ForEach-Object { (Get-Process -Id $_ -ErrorAction SilentlyContinue).WorkingSet64 } | Measure-Object -Sum).Sum / 1MB
"{0}: {1:N1}% of one core over {2}s ({3} processes, {4:N0} MB RAM)" -f $Match, (($b - $a) / $Seconds * 100), $Seconds, @($ids).Count, $ram
