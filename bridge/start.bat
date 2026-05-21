@echo off
echo ============================================
echo  OpenAlgo Claude Bridge Server
echo  Starting on http://127.0.0.1:5001
echo ============================================
echo.
cd /d "%~dp0.."
pip install fastapi --quiet 2>nul
uv run python bridge/server.py
