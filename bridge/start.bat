@echo off
echo ============================================
echo  OpenAlgo Claude Bridge Server
echo  Starting on http://127.0.0.1:5001
echo ============================================
echo.
echo Checking for existing process on port 5001...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5001 " ^| findstr "LISTENING"') do (
  echo Killing PID %%a holding port 5001
  taskkill /F /PID %%a 2>nul
)
timeout /t 2 /nobreak >nul
cd /d "%~dp0.."
pip install fastapi --quiet 2>nul
uv run python bridge/server.py
