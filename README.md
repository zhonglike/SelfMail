<div align="center">

# SelfMail 自邮

**一台 Windows/Linux 主机，跑起属于你自己的邮件服务器。**
零框架 · 单依赖 · 液态玻璃 Webmail · 59 项端到端实测全绿

`SMTP` · `IMAP` · `POP3` · `Webmail` · `DKIM 签名` · `自动配置` · `纯 Python 标准库`

</div>

## 它是什么

SelfMail 是一个自托管邮件服务器，把邮件系统最难的「全家桶」打包成单进程：

- **SMTP**（25 MX 收信 / 465 隐式 TLS / 587 STARTTLS 提交）+ 出站队列重试与退信
- **IMAP4rev1**（993/143，ENVELOPE/BODYSTRUCTURE/IDLE 实时推送/中文文件夹）
- **POP3**（995/110）
- **Webmail + 管理后台**（iOS 风格液态玻璃 UI，黑白极简，跟随系统深色）
- **DKIM 签名**、SPF/DMARC 记录生成与在线核查
- **Thunderbird/Outlook 自动配置**（autoconfig + Autodiscover 端点）
- **配额、别名、管理员、失败封禁限流、CSRF、HTML 消毒**

除 `cryptography`（RSA/DKIM）与 `dkimpy`（测试用独立验签）外，运行时不依赖任何第三方库。

## 快速开始

```powershell
# Windows（管理员 PowerShell，在项目目录）
powershell -ExecutionPolicy Bypass -File scripts\win\install-windows.ps1
```

```bash
# Linux
python3 -m venv .venv && .venv/bin/pip install cryptography dkimpy
.venv/bin/python -m selfmail init && .venv/bin/python -m selfmail serve
```

然后按 `docs/部署指南.md` 配好 DNS 六件套，用浏览器打开 `https://你的主机` 即可。

文档：[部署指南](docs/部署指南.md) · [客户端配置](docs/客户端配置.md) · [安全加固](docs/安全加固.md) · [架构说明](docs/架构说明.md)

## 安装包（dist/）

| 平台 | 文件 | 用法 |
|---|---|---|
| Windows | `dist/windows/SelfMail-Windows-1.0.0.zip` | 解压后双击 `SelfMail.exe`：自动建环境/装依赖/生成配置与管理员/放行防火墙/注册开机自启，然后启动服务 |
| Ubuntu/Debian | `dist/ubuntu/SelfMail_1.0.0_all.deb` | `sudo dpkg -i selfmail_1.0.0_all.deb`（离线装依赖）→ `sudo selfmail init` → `sudo systemctl start selfmail` |
| Android | `dist/android/SelfMail-1.0.0.apk` | 传到手机安装，首启输入服务器地址即可（v2+v3 签名，Android 7.0+） |

Webmail 亦支持 PWA：手机浏览器打开后"添加到主屏幕"即可类 App 使用。
重新构建安装包：`scripts/apk/build-apk.sh`（APK）、`scripts/win/`（Windows）、deb 构建树见仓库。

## 测试

```bash
python tests/smoke_test.py   # 59 项端到端：SMTP/IMAP/POP3/Web/DKIM/配额/别名/队列
```

测试在临时目录拉起真实服务器进程，用标准库客户端（smtplib/imaplib/poplib）与
Web API 全链路验证，并用 dkimpy 独立验签 DKIM 签名。

## License

MIT — 见 [LICENSE](LICENSE)
