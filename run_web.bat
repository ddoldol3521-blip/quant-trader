@echo off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
start "" http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 --server.headless true
pause
