$ErrorActionPreference = "Stop"

$Python = (Get-Command python.exe).Source
$Scripts = "C:\Scripts"

Write-Host "Python: $Python"

# TikTok LIVE監視
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "$Scripts\record_tiktok_live.py --continuous --stall-seconds 60" `
    -WorkingDirectory $Scripts

$Trigger = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName "TikTok_moa_pi_ライブ録画" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "TikTok moa_pi LIVE監視録画" `
    -Force

# ラジオ川越 毎週日曜23時
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "$Scripts\record_radiokawagoe.py --duration 1800 --outdir F:\Backup\Recordings" `
    -WorkingDirectory $Scripts

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At 23:00

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName "ラジオ川越_日曜23時録音" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "ラジオ川越 毎週日曜23時録音" `
    -Force

Write-Host "タスク登録完了"
