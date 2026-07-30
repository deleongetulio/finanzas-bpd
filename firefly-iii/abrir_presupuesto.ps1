# Un solo doble-click: prende Docker si hace falta, espera a que Firefly
# responda, y abre el navegador en localhost:8080.
function DockerListo {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (DockerListo)) {
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $intentos = 0
    while (-not (DockerListo) -and $intentos -lt 24) {
        Start-Sleep -Seconds 5
        $intentos++
    }
}

Set-Location $PSScriptRoot
$appStatus = docker inspect --format='{{.State.Running}}' firefly_iii_core 2>$null
if ($appStatus -ne "true") {
    docker compose up -d *> $null
}

# esperar a que la app realmente responda antes de abrir el navegador
$listo = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $listo = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}

Start-Process "http://localhost:8080"
