# Telegram_Digest.exe — Unofficial TelegramNewsAI

`Telegram_Digest.exe` — небольшой открытый Windows launcher. Он находит `start_free.bat` рядом с собой и запускает его в отдельном `cmd.exe /d /s /c`, поэтому консоль закрывается после завершения Python.

Исходник не содержит Telegram API-данных и не подключается к сети. Launcher не читает `credentials.bin`, сессию или базу.

## Сборка

На 64-битной Windows с установленным .NET Framework 4 выполните из корня репозитория:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\build_launcher.ps1
```

Сценарий использует штатный компилятор `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe` и исходник `Telegram_Digest.cs`. `build_icon.ps1` программно рисует упрощённый проектный значок и собирает многоразмерный Windows ICO (16–256 px), оптимизированный для маленьких размеров Проводника и ярлыков. `build_launcher.ps1` встраивает ICO в EXE через `/win32icon`. Для README используется согласованная векторная версия `assets/icon.svg`. Значок оригинальный и не копирует официальный логотип Telegram.

После сборки обновите `Telegram_Digest.exe.sha256` и выполните:

```powershell
.\Telegram_Digest.exe --self-test
```

Самопроверка не запускает Unofficial TelegramNewsAI и не обращается к Telegram.

