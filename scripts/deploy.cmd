@echo off
cd /d "%~dp0.."
if "%~1"=="" (
  py -3 serve.py run
  if errorlevel 1 python serve.py run
  exit /b %ERRORLEVEL%
)
py -3 serve.py %*
if errorlevel 1 python serve.py %*
exit /b %ERRORLEVEL%
