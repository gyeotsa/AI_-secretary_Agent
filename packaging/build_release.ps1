param(
    [switch]$SkipAppBuild,
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$releaseRoot = Join-Path $repoRoot 'release'
$payloadRoot = Join-Path $releaseRoot 'JARVIS-Payload'
$ollamaSource = Join-Path $env:USERPROFILE '.ollama\models'
$requiredPaths = @(
    (Join-Path $repoRoot 'data\models\bge-m3'),
    (Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--Systran--faster-whisper-large-v3'),
    (Join-Path $env:USERPROFILE '.cache\whisper\medium.pt'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama'),
    (Join-Path $env:LOCALAPPDATA 'ms-playwright')
)
$defaultOllamaModels = @(
    'qwen2.5-coder:7b-instruct',
    'qwen2.5:7b-instruct',
    'gemma3:4b',
    'qwen2.5vl:7b'
)
foreach ($model in $defaultOllamaModels) {
    $parts = $model.Split(':', 2)
    $requiredPaths += Join-Path $ollamaSource ("manifests\registry.ollama.ai\library\{0}\{1}" -f $parts[0], $parts[1])
}
$missingPaths = @($requiredPaths | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missingPaths.Count -gt 0) {
    throw "오프라인 릴리스 필수 파일이 없습니다:`n$($missingPaths -join "`n")"
}

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

# 기본 역할(대화·추론·코딩·Vision·디자인 Vision)에 필요한 manifest와 참조 blob을 포함한다.
foreach ($model in $defaultOllamaModels) {
    $parts = $model.Split(':', 2)
    $manifestRelative = "manifests\registry.ollama.ai\library\$($parts[0])\$($parts[1])"
    $manifestSource = Join-Path $ollamaSource $manifestRelative
    $manifestDestination = Join-Path (Join-Path $internal 'models\ollama') $manifestRelative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $manifestDestination) | Out-Null
    Copy-Item -LiteralPath $manifestSource -Destination $manifestDestination -Force
    $manifest = Get-Content -Raw -LiteralPath $manifestSource | ConvertFrom-Json
    $digests = @($manifest.config.digest) + @($manifest.layers | ForEach-Object { $_.digest })
    foreach ($digest in $digests) {
        $blobName = $digest.Replace(':', '-')
        $blobSource = Join-Path $ollamaSource "blobs\$blobName"
        if (-not (Test-Path -LiteralPath $blobSource)) {
            throw "Ollama 모델 blob이 없습니다: $model / $blobSource"
        }
        $blobDestination = Join-Path $internal "models\ollama\blobs\$blobName"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $blobDestination) | Out-Null
        if (-not (Test-Path -LiteralPath $blobDestination)) {
            Copy-Item -LiteralPath $blobSource -Destination $blobDestination
        }
    }
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
