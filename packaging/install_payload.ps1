$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\JARVIS'

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
Get-ChildItem -LiteralPath $sourceRoot -Force | Where-Object {
    $_.Name -notin @('install_payload.ps1', 'uninstall_payload.ps1')
} | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $installRoot -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $sourceRoot 'uninstall_payload.ps1') -Destination $installRoot -Force

$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\JARVIS'
New-Item -Path $uninstallKey -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name DisplayName -Value 'JARVIS AI Assistant' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name DisplayVersion -Value '1.0.0' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name Publisher -Value 'JARVIS' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name DisplayIcon -Value (Join-Path $installRoot 'JARVIS.exe') -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name InstallLocation -Value $installRoot -PropertyType String -Force | Out-Null
$uninstallCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $installRoot 'uninstall_payload.ps1') + '"'
New-ItemProperty -Path $uninstallKey -Name UninstallString -Value $uninstallCommand -PropertyType String -Force | Out-Null

$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $shell.CreateShortcut((Join-Path $desktop 'JARVIS.lnk'))
$shortcut.TargetPath = Join-Path $installRoot 'JARVIS.exe'
$shortcut.WorkingDirectory = $installRoot
$shortcut.IconLocation = (Join-Path $installRoot '_internal\assets\jarvis.ico') + ',0'
$shortcut.Save()

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$menuShortcut = $shell.CreateShortcut((Join-Path $startMenu 'JARVIS.lnk'))
$menuShortcut.TargetPath = Join-Path $installRoot 'JARVIS.exe'
$menuShortcut.WorkingDirectory = $installRoot
$menuShortcut.IconLocation = (Join-Path $installRoot '_internal\assets\jarvis.ico') + ',0'
$menuShortcut.Save()

Start-Process -FilePath (Join-Path $installRoot 'JARVIS.exe') -WorkingDirectory $installRoot
