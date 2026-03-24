@echo off
cd /d "%~dp0"
call "%~dp0scripts\deploy.cmd" run %*
set "EC=%ERRORLEVEL%"
pause
exit /b %EC%
