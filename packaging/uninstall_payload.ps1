$ErrorActionPreference = 'SilentlyContinue'
$installRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Remove-Item -LiteralPath ([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'JARVIS.lnk')) -Force
Remove-Item -LiteralPath (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\JARVIS.lnk') -Force
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\JARVIS' -Recurse -Force
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-Command', "Start-Sleep -Seconds 2; Remove-Item -LiteralPath '$installRoot' -Recurse -Force"
)
