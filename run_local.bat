@echo off
setlocal
cd /d "%~dp0"

echo ==============================================
echo  Findchips Purchasing Matcher - Local Test
echo ==============================================

where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py"
) else (
    set "PY=python"
)

%PY% -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo Installing Playwright...
    %PY% -m pip install --user "playwright>=1.55,<2"
)

set "HOST=127.0.0.1"
set "PORT=8765"
set "FINDCHIPS_NO_OPEN=0"

cd backend
%PY% app.py

pause
