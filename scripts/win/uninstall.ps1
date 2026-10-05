# 卸载 SelfMail（管理员 PowerShell）：停任务、删防火墙规则、删计划任务
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "SelfMail" -Confirm:$false
Unregister-ScheduledTask -TaskName "SelfMail-DDNS" -Confirm:$false
foreach ($p in @(25, 465, 587, 143, 993, 110, 995, 443)) {
    Remove-NetFirewallRule -DisplayName "SelfMail-$p"
}
Write-Host "SelfMail 计划任务与防火墙规则已移除（data 目录保留，如需彻底删除请手动清理）"
