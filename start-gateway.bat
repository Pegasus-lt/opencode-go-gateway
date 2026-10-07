@echo off
setlocal
title ogo-gw - OpenCode Go local gateway

set "PORT=8787"
set "UP=https://opencode.ai/zen/go"
set "HERE=%~dp0"
set "PY="
set "PROBE=%HERE%_probe.py"

REM ---- locate python (path may contain non-ASCII, so keep quotes simple) ----
if not defined PY for %%P in (python py) do if not defined PY (%%P --version >nul 2>&1 && set "PY=%%P")

if not defined PY (
  echo.
  echo   [X] Python not found. Install Python 3 and re-run, or run manually:
  echo.
  echo   python "%HERE%gateway.py" -v
  echo.
  pause
  exit /b 1
)

REM ---- port check ----
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo.
  echo   [!] Port %PORT% already in use. Checking whether it is ogo-gw...
  echo.
  "%PY%" "%PROBE%" http://127.0.0.1:%PORT%/_health
  echo.
  echo   JSON above  = ogo-gw is running, point your client at it.
  echo   No output   = something else owns this port, edit PORT= below.
  echo.
  pause
  exit /b 0
)

echo.
echo   ogo-gw starting...
echo     upstream: %UP%
echo     local:    http://127.0.0.1:%PORT%/v1
echo.
echo   Point your client's Base URL at it:
echo     URL       http://127.0.0.1:%PORT%/v1
echo     API Key   your OpenCode Go key
echo     Model ID  deepseek-v4-flash
echo.
echo   IMPORTANT: fully restart your client after changing its config.
echo   Stop: close this window or press Ctrl+C.
echo   ---------------------------------------------
echo.

"%PY%" "%HERE%gateway.py" --port %PORT% --upstream %UP% -v

echo.
echo   Gateway stopped. Clients cannot connect now.
pause