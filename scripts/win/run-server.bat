@echo off
rem SelfMail 服务启动器（计划任务/手动运行均可）
cd /d "%~dp0..\.."
.venv\Scripts\python.exe -m selfmail serve
