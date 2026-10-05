# SelfMail Windows 一键部署脚本
# 用法（管理员 PowerShell）：powershell -ExecutionPolicy Bypass -File install-windows.ps1
# 功能：Python 检查 -> 虚拟环境 -> 依赖 -> 防火墙 -> 开机自启计划任务 -> 初始化账号

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot   # 项目根（scripts/ 的上一级）
Set-Location $Root
Write-Host "SelfMail 自邮 · Windows 部署 —— 项目根: $Root" -ForegroundColor Cyan

# ---------- 1. Python ----------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "未检测到 Python，请先安装 Python 3.10+（勾选 Add to PATH）：https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "Python 版本: $v"

# ---------- 2. 虚拟环境与依赖 ----------
if (!(Test-Path ".venv")) { python -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip -q
& .venv\Scripts\python.exe -m pip install cryptography dkimpy -q
Write-Host "依赖安装完成"

# ---------- 3. 防火墙放行 ----------
$ports = @(25, 465, 587, 143, 993, 110, 995, 443)
foreach ($p in $ports) {
    $rule = "SelfMail-$p"
    if (!(Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow | Out-Null
        Write-Host "防火墙已放行 TCP $p"
    }
}

# ---------- 4. 公网路由器端口转发提醒 ----------
Write-Host @"

【重要】若服务器在路由器/NAT 之后，还需在路由器管理页做端口转发：
  外部 TCP 25,465,587 -> 本机 (如 192.168.x.x) 同端口
  外部 TCP 443,993,995 -> 本机 同端口
完成后用 https://mxtoolbox.com 检查 25 端口是否可达。

"@ -ForegroundColor Yellow

# ---------- 5. 初始化配置与管理员 ----------
if (!(Test-Path "data\config.json")) {
    $domain = Read-Host "请输入邮件域名 (如 example.com)"
    $hostname = Read-Host "请输入主机名 (如 mail.example.com)"
    New-Item -ItemType Directory -Force -Path "data" | Out-Null
    & .venv\Scripts\python.exe -c "
import json, sys; sys.path.insert(0, '.')
from selfmail import config
d = json.loads(json.dumps(config.DEFAULTS))
d['domain'] = '$domain'; d['hostname'] = '$hostname'
from pathlib import Path
p = Path('data/config.json'); p.write_text(json.dumps(d, indent=2))
print('config-ok')
"
}
$admin = Read-Host "管理员账号 (如 admin)"
$adminPass = Read-Host "管理员密码" -AsSecureString
$adminPassText = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($adminPass))
& .venv\Scripts\python.exe -m selfmail adduser $admin $adminPassText --admin

# ---------- 6. 开机自启（计划任务） ----------
$taskName = "SelfMail"
$runner = Join-Path $Root "scripts\win\run-server.bat"
$action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "已注册开机自启任务 SelfMail（失败自动重启）"

# ---------- 7. DNS 记录提示 ----------
& .venv\Scripts\python.exe -m selfmail gen-dns
Write-Host @"

部署完成！
  立即启动:   scripts\win\run-server.bat   或   Start-ScheduledTask SelfMail
  Webmail:   https://本机IP:443
  DDNS:      参考 scripts\win\ddns-cloudflare.ps1（动态公网 IP 必配）

"@ -ForegroundColor Green
