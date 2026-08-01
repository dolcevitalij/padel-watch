Set-Location $PSScriptRoot

$changed = git status --porcelain config.yaml
if (-not $changed) {
    Write-Host "Keine Aenderungen an config.yaml - nichts zu tun."
    return
}

git add config.yaml
git commit -m "Konfiguration aktualisiert"
git push

Write-Host ""
Write-Host "Fertig. Der naechste GitHub-Actions-Lauf nutzt diese Konfiguration."
