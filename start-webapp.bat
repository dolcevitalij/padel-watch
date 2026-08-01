@echo off
cd /d "%~dp0"
echo Starte Padel-Watch Testkonsole auf http://localhost:5000 ...
echo Zum Beenden: dieses Fenster schliessen oder Strg+C.
echo.
".venv\Scripts\python.exe" webapp.py
pause
