param(
    [switch]$SkipAppBuild,
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$releaseRoot = Join-Path $repoRoot 'release'
$payloadRoot = Join-Path $releaseRoot 'JARVIS-Payload'

if (-not $SkipAppBuild) {
    & $python -m PyInstaller --clean --noconfirm (Join-Path $PSScriptRoot 'Jarvis.spec') --distpath (Join-Path $repoRoot 'dist') --workpath (Join-Path $repoRoot 'build\pyinstaller')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
}

if (Test-Path -LiteralPath $payloadRoot) { Remove-Item -LiteralPath $payloadRoot -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $repoRoot 'dist\JARVIS') -Destination $payloadRoot -Recurse
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install_payload.ps1') -Destination $payloadRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall_payload.ps1') -Destination $payloadRoot

$internal = Join-Path $payloadRoot '_internal'
New-Item -ItemType Directory -Force -Path (Join-Path $internal 'models') | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot 'data\models\bge-m3') -Destination (Join-Path $internal 'models\bge-m3') -Recurse
$hfHubDestination = Join-Path $internal 'models\huggingface\hub'
New-Item -ItemType Directory -Force -Path $hfHubDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--Systran--faster-whisper-large-v3') -Destination $hfHubDestination -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $internal 'models\whisper') | Out-Null
Copy-Item -LiteralPath (Join-Path $env:USERPROFILE '.cache\whisper\medium.pt') -Destination (Join-Path $internal 'models\whisper\medium.pt')
Copy-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\Ollama') -Destination (Join-Path $internal 'runtime\ollama') -Recurse
Copy-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'ms-playwright') -Destination (Join-Path $internal 'runtime\ms-playwright') -Recurse

# 현재 설정에서 사용하는 Ollama 모델의 manifest와 참조 blob만 포함한다.
$ollamaSource = Join-Path $env:USERPROFILE '.ollama\models'
$manifestRelative = 'manifests\registry.ollama.ai\library\qwen2.5-coder\7b-instruct'
$manifestSource = Join-Path $ollamaSource $manifestRelative
$manifestDestination = Join-Path (Join-Path $internal 'models\ollama') $manifestRelative
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $manifestDestination) | Out-Null
Copy-Item -LiteralPath $manifestSource -Destination $manifestDestination
$manifest = Get-Content -Raw -LiteralPath $manifestSource | ConvertFrom-Json
$digests = @($manifest.config.digest) + @($manifest.layers | ForEach-Object { $_.digest })
foreach ($digest in $digests) {
    $blobName = $digest.Replace(':', '-')
    $blobSource = Join-Path $ollamaSource "blobs\$blobName"
    $blobDestination = Join-Path $internal "models\ollama\blobs\$blobName"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $blobDestination) | Out-Null
    Copy-Item -LiteralPath $blobSource -Destination $blobDestination
}

Write-Output "Payload ready: $payloadRoot"

if (-not $SkipInstaller) {
    $iscc = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not (Test-Path -LiteralPath $iscc)) {
        throw 'Inno Setup 6이 필요합니다. winget install --id JRSoftware.InnoSetup -e 명령으로 설치하세요.'
    }
    $installerRoot = Join-Path $releaseRoot 'installer'
    if (Test-Path -LiteralPath $installerRoot) {
        Remove-Item -LiteralPath $installerRoot -Recurse -Force
    }
    & $iscc (Join-Path $PSScriptRoot 'Jarvis.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup installer creation failed.' }
    Write-Output "Offline installer folder ready: $installerRoot"
}
