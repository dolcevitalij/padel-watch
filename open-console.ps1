# Wartet, bis die Testkonsole wirklich antwortet, und oeffnet sie dann in Chrome.
#
# Das Warten ist der Punkt: Flask braucht nach dem Start ein bis zwei Sekunden,
# bis der Port lauscht. Ein sofort geoeffneter Browser zeigt sonst
# ERR_CONNECTION_REFUSED, obwohl alles in Ordnung ist.
#
# Wird von start-webapp.bat im Hintergrund aufgerufen, laeuft aber auch allein,
# wenn der Server schon laeuft.

$url  = "http://localhost:5000"
$port = 5000

for ($i = 0; $i -lt 40; $i++) {     # max. 20 Sekunden
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "Server antwortet nach 20 s nicht - Browser wird nicht geoeffnet."
    Write-Host "Fehlermeldung steht im Konsolenfenster von start-webapp.bat."
    exit 1
}

# Chrome an den ueblichen Orten suchen; sonst Standardbrowser
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($chrome) {
    Start-Process -FilePath $chrome -ArgumentList $url
} else {
    Write-Host "Chrome nicht gefunden - oeffne den Standardbrowser."
    Start-Process $url
}
