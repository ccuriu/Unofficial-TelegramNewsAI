@echo off
chcp 65001 >nul

if not exist "%~dp0credentials.bin" (
    echo.
    echo === Первичная настройка Telegram ===
    echo API ID и API Hash можно получить: https://my.telegram.org ^> API development tools
    echo Важно: API HASH и пароль двухэтапной защиты вводятся скрытно.
    echo Символы на экране не отображаются — это нормально. После ввода или вставки нажмите Enter.
    echo.
)

call "%~dp0run_python.bat" "%~dp0telegram_collector_free.py"
set "TELEGRAMNEWSAI_EXIT_CODE=%ERRORLEVEL%"
exit /b %TELEGRAMNEWSAI_EXIT_CODE%
