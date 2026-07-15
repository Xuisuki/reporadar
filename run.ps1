# RepoRadar — запуск под Windows. Один вызов = один пост.
#   .\run.ps1            один пост
#   .\run.ps1 preview    сухой прогон
Set-Location -Path $PSScriptRoot
$VPY = ".venv\Scripts\python.exe"
if (-not (Test-Path $VPY)) { Write-Host "Сначала запустите install.ps1" -ForegroundColor Red; exit 1 }
$cmd = if ($args.Count -gt 0) { $args } else { @("run") }
& $VPY bot.py @cmd
