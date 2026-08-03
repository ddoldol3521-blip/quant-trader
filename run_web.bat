@echo off
REM ASCII only. Korean characters break batch files on this system.
REM Same fix as run_jongsa.bat -- Streamlit opens the browser when ready.
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1

echo.
echo   Starting... the browser opens by itself in a few seconds.
echo   KEEP THIS WINDOW OPEN while you use the app.
echo   Address: http://localhost:8501
echo.

".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501

echo.
echo   Stopped. You can close this window.
pause
