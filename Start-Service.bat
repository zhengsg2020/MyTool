@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  python "%~dp0serve.py" run
) else (
  python "%~dp0serve.py" %*
)

set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" pause
exit /b %EC%
