@{
    ProgramFiles = @(
        'INSTALL.bat'
        'install.ps1'
        'UPDATE.bat'
        'update.ps1'
        'release_manifest.psd1'
        'README.md'
        'LICENSE'
        'SECURITY.md'
        'requirements.txt'
        'run_python.bat'
        'start_free.bat'
        'telegram_collector_free.py'
        'Telegram_Digest.exe'
        'Telegram_Digest.exe.sha256'
    )

    # Сюда добавляются только точно известные устаревшие файлы программы.
    # Неизвестные файлы пользователя updater никогда не удаляет.
    ObsoleteFiles = @(
    )
}
