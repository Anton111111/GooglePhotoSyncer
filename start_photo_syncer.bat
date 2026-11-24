@echo off
cd /d "%~dp0"
python "%~dp0photo_syncer.py"
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
