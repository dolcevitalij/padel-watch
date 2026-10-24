@echo off
cd /d "%~dp0"

REM Laeuft schon eine Instanz? Dann keine zweite starten - zwei Prozesse auf
REM demselben Port fuehren zu unerklaerlichem Verhalten (die zweite bindet nicht
REM und die alte antwortet weiter, ggf. mit veralteter .env).
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 goto laeuftschon

REM Erst den Stand von GitHub holen: wurde online gespeichert, zeigte die
REM Konsole sonst die alte config.yaml - und Speichern + publish-config
REM wuerde die Online-Aenderung ueberschreiben oder in einen Konflikt laufen.
echo Hole aktuellen Stand von GitHub ...
git pull --rebase --autostash -q
if errorlevel 1 (
    echo.
    echo WARNUNG: git pull fehlgeschlagen - die Konsole zeigt evtl. einen veralteten Stand.
    echo.
)

echo Starte Padel-Watch Testkonsole auf http://localhost:5000 ...
echo Chrome oeffnet sich automatisch, sobald der Server antwortet.
echo Zum Beenden: dieses Fenster schliessen oder Strg+C.
echo.

REM Browser im Hintergrund oeffnen, sobald der Port lauscht
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-console.ps1"

".venv\Scripts\python.exe" webapp.py
pause
exit /b

:laeuftschon
echo Auf Port 5000 laeuft bereits eine Konsole.
echo Es wird keine zweite gestartet - nur der Browser wird geoeffnet.
echo.
echo Falls diese Instanz veraltet ist (z.B. nach Aenderungen an der .env):
echo     Get-Process python ^| Stop-Process -Force
echo und danach diese Datei erneut starten.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-console.ps1"
pause
