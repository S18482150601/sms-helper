@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 短信助手本地服务

where node >nul 2>nul
if errorlevel 1 (
  echo.
  echo [错误] 没有检测到 Node.js
  echo 请先到 https://nodejs.org 下载安装 LTS 版本，安装时一路默认即可。
  echo.
  pause
  exit /b 1
)

if not exist "config.json" (
  copy "config.example.json" "config.json" >nul
  echo 已生成 config.json，默认是模拟模式（不会真发短信）。
  echo 要真发，请用记事本打开 server\config.json 填写通道信息后重开本文件。
  echo.
)

node server.js
pause
