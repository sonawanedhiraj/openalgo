@echo off
echo ============================================
echo  OpenAlgo Platform
echo  Starting on http://127.0.0.1:5000 (WS 8765)
echo ============================================
echo.
echo Checking for existing processes on ports 5000 and 8765...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 " ^| findstr "LISTENING"') do (
  echo Killing PID %%a holding port 5000
  taskkill /F /PID %%a 2>nul
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765 " ^| findstr "LISTENING"') do (
  echo Killing PID %%a holding port 8765
  taskkill /F /PID %%a 2>nul
)
timeout /t 3 /nobreak >nul
cd /d "%~dp0.."
echo Starting OpenAlgo (logs: log\openalgo_stdout.log / log\openalgo_stderr.log)...
uv run python app.py > "log\openalgo_stdout.log" 2> "log\openalgo_stderr.log"
