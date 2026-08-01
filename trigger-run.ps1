# Loest den Workflow per API aus - genau der Aufruf, den auch der externe
# Cron-Dienst macht. Dient dazu, Token und Aufruf einmal lokal zu pruefen,
# bevor sie beim Dienst hinterlegt werden.
#
# Token: erwartet GITHUB_TOKEN in der .env neben diesem Skript
#        (Fine-grained PAT, nur dieses Repo, Actions: Read and write).

Set-Location $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) { Write-Host "FEHLER: .env nicht gefunden."; return }

$token = $null
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*GITHUB_TOKEN\s*=\s*(.+?)\s*$') { $token = $Matches[1].Trim('"').Trim("'") }
}
if (-not $token) {
    Write-Host "FEHLER: GITHUB_TOKEN steht nicht in der .env."
    Write-Host "Zeile ergaenzen:  GITHUB_TOKEN=github_pat_..."
    return
}

$repo = "dolcevitalij/padel-watch"
$url  = "https://api.github.com/repos/$repo/actions/workflows/padel-watch.yml/dispatches"

Write-Host "POST $url"
try {
    $r = Invoke-WebRequest -Uri $url -Method POST `
        -Headers @{
            "Accept"               = "application/vnd.github+json"
            "Authorization"        = "Bearer $token"
            "X-GitHub-Api-Version" = "2022-11-28"
            "User-Agent"           = "padel-watch-cron"
        } `
        -Body '{"ref":"main"}' -ContentType "application/json" -TimeoutSec 30
    Write-Host "HTTP $($r.StatusCode) - erwartet ist 204 (kein Inhalt) = Lauf gestartet."
    Write-Host "Kontrolle: https://github.com/$repo/actions"
} catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { "-" }
    Write-Host "FEHLGESCHLAGEN (HTTP $code): $($_.Exception.Message)"
    Write-Host ""
    Write-Host "401/403 -> Token fehlt die Berechtigung 'Actions: Read and write'"
    Write-Host "           oder es ist nicht fuer dieses Repo freigegeben."
    Write-Host "404     -> Repo-Name oder Workflow-Dateiname falsch, oder Token"
    Write-Host "           sieht das Repo nicht."
}
