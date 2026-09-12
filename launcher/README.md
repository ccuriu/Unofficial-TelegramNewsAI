# Telegram_Digest.exe

`Telegram_Digest.exe` — небольшой открытый Windows launcher. Он находит `start_free.bat` рядом с собой и запускает его в отдельном `cmd.exe /d /s /c`, поэтому консоль закрывается после завершения Python.

Исходник не содержит Telegram API-данных и не подключается к сети. Launcher не читает `credentials.bin`, сессию или базу.

## Сборка

На 64-битной Windows с установленным .NET Framework 4 выполните из корня репозитория:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\build_launcher.ps1
```

Сценарий использует штатный компилятор `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`, исходник `Telegram_Digest.cs` и `TelegramNewsAI.ico`. Результат записывается в корневой `Telegram_Digest.exe`.

После сборки обновите `Telegram_Digest.exe.sha256` и выполните:

```powershell
.\Telegram_Digest.exe --self-test
```

Самопроверка не запускает TelegramNewsAI и не обращается к Telegram.

