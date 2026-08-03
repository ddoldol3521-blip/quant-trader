@echo off
REM ASCII only. Korean characters break batch files on this system.
REM Let Streamlit open the browser ITSELF when the server is ready.
REM The old version opened the browser first, so an already-running browser
REM hit the port before the server was up and showed "cannot connect".
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1

echo.
echo   Starting... the browser opens by itself in a few seconds.
echo   KEEP THIS WINDOW OPEN while you use the app.
echo   Address: http://localhost:8502
echo.

".venv\Scripts\python.exe" -m streamlit run jongsa_app.py --server.port 8502

echo.
echo   Stopped. You can close this window.
pause
