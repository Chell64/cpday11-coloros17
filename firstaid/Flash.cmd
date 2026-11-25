@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\chcp.com" 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
"%~dp0tools\python\python.exe" -B -u -X utf8 "%~dp0tools\python\launch_firstaid.py" %*
exit /b %errorlevel%
