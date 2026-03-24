@echo off
setlocal
cd /d "%~dp0"
python "%~dp0stop_service.py"
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" pause
exit /b %EC%
