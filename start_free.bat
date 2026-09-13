@echo off
chcp 65001 >nul
call "%~dp0run_python.bat" "%~dp0telegram_collector_free.py"
set "TELEGRAMNEWSAI_EXIT_CODE=%ERRORLEVEL%"
exit /b %TELEGRAMNEWSAI_EXIT_CODE%
