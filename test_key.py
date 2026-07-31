"""
Einmaliger Test: Funktioniert der Abruf inkl. key-Extraktion?

Ausfuehren mit:   python test_key.py

Kein Telegram, keine Secrets noetig - es wird nur einmal die Belegung
fuer ein Datum in ein paar Tagen abgerufen und ausgewertet.
"""
import datetime as dt
from fetch import fetch_grid, extract_key, BASE, HEADERS
import requests

ID_CUADRO = "4"

# Datum automatisch: heute + 3 Tage, im Matchpoint-Format T/M/JJJJ
d = dt.date.today() + dt.timedelta(days=3)
fecha = f"{d.day}/{d.month}/{d.year}"

print(f"Teste Abruf fuer {fecha} (idCuadro={ID_CUADRO}) ...\n")

# --- Schritt 1: laesst sich der key aus der Seite ziehen? ---
try:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    html = sess.get(f"{BASE}/Booking/Grid.aspx?id={ID_CUADRO}", timeout=20).text
    key = extract_key(html)
    if key:
        print(f"[OK]   key automatisch gefunden: {key[:12]}... (Laenge {len(key)})")
    else:
        print("[FEHLER] key NICHT gefunden.")
        print("         -> Grid-Seite im Browser oeffnen, Quelltext ansehen,")
        print("            den key-String suchen und mir den Kontext schicken.")
except Exception as e:
    print(f"[FEHLER] Seite konnte nicht geladen werden: {e}")

# --- Schritt 2: kompletter Abruf + Auswertung ---
print()
try:
    data = fetch_grid(ID_CUADRO, fecha)
    d2 = data["d"]
    courts = d2.get("Columnas", [])
    print(f"[OK]   Abruf erfolgreich. Plan: '{d2.get('Nombre')}', "
          f"{len(courts)} Courts, Datum {d2.get('StrFecha')}")
    for c in courts:
        print(f"         [{c['Id']}] {c['TextoPrincipal']}: "
              f"{len(c.get('Ocupaciones', []))} Belegungen")
    print("\n=> Alles funktioniert. Du kannst zur GitHub-Einrichtung uebergehen.")
except Exception as e:
    print(f"[FEHLER] Abruf fehlgeschlagen: {e}")
    print("         -> Wahrscheinlich key-Problem oder Login noetig. Meld dich.")
