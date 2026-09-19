# Telegram_Digest.exe — Unofficial TelegramNewsAI

`Telegram_Digest.exe` — небольшой открытый Windows launcher. Он находит `start_free.bat` рядом с собой и запускает его в отдельном `cmd.exe /d /s /c`, поэтому консоль закрывается после завершения Python.

Исходник не содержит Telegram API-данных и не подключается к сети. Launcher не читает `credentials.bin`, сессию или базу.

## Сборка

На 64-битной Windows с установленным .NET Framework 4 выполните из корня репозитория:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\build_launcher.ps1
```

Сценарий использует штатный компилятор `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe` и исходник `Telegram_Digest.cs`. В 5.4.12 Testing пользовательская сборка намеренно не использует Telegram-брендинг или отдельный Telegram-подобный значок; Windows применяет стандартный значок EXE. Результат записывается в `Telegram_Digest.exe`.

После сборки обновите `Telegram_Digest.exe.sha256` и выполните:

```powershell
.\Telegram_Digest.exe --self-test
```

Самопроверка не запускает Unofficial TelegramNewsAI и не обращается к Telegram.

