# 注册 DDNS 每 10 分钟定时任务（管理员 PowerShell）
$Root = Split-Path -Parent $PSScriptRoot
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument "-ExecutionPolicy Bypass -File `"$Root\scripts\win\ddns-cloudflare.ps1`"" `
             -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "SelfMail-DDNS" -Action $action -Trigger $trigger -Force | Out-Null
Write-Host "已注册 SelfMail-DDNS 每 10 分钟更新任务"
Start-ScheduledTask "SelfMail-DDNS"
