@echo off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
start "" http://localhost:8502
".venv\Scripts\python.exe" -m streamlit run jongsa_app.py --server.port 8502 --server.headless true
pause
