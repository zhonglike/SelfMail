# Cloudflare DDNS 动态域名解析（Windows 计划任务每 10 分钟运行）
# 准备工作：Cloudflare 控制台创建 API Token（Zone.DNS 编辑权限）
#
# 手动运行： powershell -ExecutionPolicy Bypass -File ddns-cloudflare.ps1
# 注册定时： powershell -ExecutionPolicy Bypass -File register-ddns.ps1

$Token  = "<你的 Cloudflare API Token>"
$Zone   = "example.com"            # 根域名
$Record = "mail.example.com"       # A 记录主机名
$TTL    = 300

$ErrorActionPreference = "Stop"

# 1. 取本机公网 IPv4（多个来源容错）
$ip = (Invoke-RestMethod "https://api.ipify.org?format=json").ip
if (-not $ip) { $ip = (Invoke-RestMethod "https://ipv4.icanhazip.com").Trim() }
Write-Host "当前公网 IP: $ip"

$H = @{ Authorization = "Bearer $Token"; "Content-Type" = "application/json" }
$base = "https://api.cloudflare.com/client/v4"

# 2. 查询 A 记录
$list = Invoke-RestMethod -Headers $H "$base/zones?name=$Zone"
$zoneId = $list.result[0].id
$recs = Invoke-RestMethod -Headers $H "$base/zones/$zoneId/dns_records?type=A&name=$Record"
$rec = $recs.result | Select-Object -First 1

# 3. 相同则跳过，不同则更新/新建
if ($rec -and $rec.content -eq $ip) {
    Write-Host "DNS 已是最新，无需更新"
    exit 0
}
$body = @{ type = "A"; name = $Record; content = $ip; ttl = $TTL; proxied = $false } | ConvertTo-Json
if ($rec) {
    Invoke-RestMethod -Method PUT -Headers $H -Body $body "$base/zones/$zoneId/dns_records/$($rec.id)" | Out-Null
    Write-Host "已更新 A 记录 $Record -> $ip"
} else {
    Invoke-RestMethod -Method POST -Headers $H -Body $body "$base/zones/$zoneId/dns_records" | Out-Null
    Write-Host "已创建 A 记录 $Record -> $ip"
}
