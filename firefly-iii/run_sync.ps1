# Corre la sincronizacion BPD -> Firefly. Pensado para la Tarea Programada de
# Windows: si Docker/Firefly no estan arriba, los levanta primero. Tolerante a
# fallos (no truena la tarea si algo no responde, solo lo deja en el log).
$ErrorActionPreference = "Continue"
$dir = $PSScriptRoot
$log = "$dir\sync_log.txt"

function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $log -Append -Encoding utf8
}

function DockerListo {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

Log "=== Iniciando sync ==="

if (-not (DockerListo)) {
    Log "Docker no responde, abriendo Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $intentos = 0
    while (-not (DockerListo) -and $intentos -lt 24) {
        Start-Sleep -Seconds 5
        $intentos++
    }
}

if (-not (DockerListo)) {
    Log "Docker no arranco a tiempo, se cancela esta corrida."
    exit 1
}

# Levantar Firefly si el contenedor no esta corriendo
Set-Location $dir
$appStatus = docker inspect --format='{{.State.Running}}' firefly_iii_core 2>$null
if ($appStatus -ne "true") {
    Log "Contenedores de Firefly no estaban arriba, corriendo docker compose up -d..."
    docker compose up -d 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 20
}

Log "Corriendo sync_all.py..."
$env:PYTHONUTF8 = "1"
python sync_all.py --dias 10 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
Log "=== Sync terminado ==="
