@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" %*
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 %*
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python %*
  exit /b
)
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
  "%LocalAppData%\Programs\Python\Python313\python.exe" %*
  exit /b
)
echo Python 3 not found. Install Python 3 with the Python launcher.
pause
exit /b 1
