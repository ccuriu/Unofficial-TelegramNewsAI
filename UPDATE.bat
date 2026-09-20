@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Обновление Unofficial TelegramNewsAI
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
if errorlevel 1 (
  echo.
  echo Обновление не завершено. Текст ошибки указан выше.
  pause
  exit /b 1
)
echo.
pause
