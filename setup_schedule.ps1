# 在 Windows 工作排程器建立「每天 12:30 執行 citydao_auto_draw.py」的工作
$dir = $PSScriptRoot
$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Error "找不到 pythonw，請先安裝 Python 並勾選 Add python.exe to PATH"; exit 1 }
if (-not (Test-Path "$dir\citydao_secret.json")) { Write-Warning "還沒建立 citydao_secret.json，排程執行時會登入失敗" }

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$dir\citydao_auto_draw.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At '12:30'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName 'citydao 老闆發瘋每日抽' -Action $action -Trigger $trigger -Settings $settings -Description '每天 12:30 自動抽限抽套組、換鑽石、開寶箱' -Force | Out-Null

$next = (Get-ScheduledTask -TaskName 'citydao 老闆發瘋每日抽' | Get-ScheduledTaskInfo).NextRunTime
Write-Host "已設定完成，下次執行時間：$next"
