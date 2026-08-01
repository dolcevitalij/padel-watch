Set-Location $PSScriptRoot

$changed = git status --porcelain config.yaml
if (-not $changed) {
    Write-Host "Keine Aenderungen an config.yaml - nichts zu tun."
    return
}

git add config.yaml
git commit -m "Konfiguration aktualisiert"
if ($LASTEXITCODE -ne 0) { Write-Host "FEHLER: commit fehlgeschlagen."; return }

# Der Actions-Job schreibt state.json ins Repo zurueck. Steht so ein Bot-Commit
# aus, lehnt GitHub den Push ab - deshalb vorher immer rebasen.
Write-Host ""
Write-Host "Hole Aenderungen von GitHub (state.json des letzten Laufs) ..."
git pull --rebase
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "FEHLER beim Zusammenfuehren. Nicht veroeffentlicht."
    Write-Host "Der Commit ist lokal vorhanden. Bitte melden - Konflikt muss von Hand geloest werden."
    return
}

git push
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "FEHLER: push fehlgeschlagen - die Konfiguration ist NICHT auf GitHub."
    Write-Host "Der naechste Lauf nutzt weiterhin die alte Konfiguration."
    return
}

Write-Host ""
Write-Host "Veroeffentlicht. Der naechste GitHub-Actions-Lauf nutzt diese Konfiguration."
