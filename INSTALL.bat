@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка TelegramNewsAI
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Установка не завершена. Текст ошибки указан выше.
  pause
  exit /b 1
)
echo.
echo Установка завершена. Можно запускать Telegram_Digest.exe.
pause

