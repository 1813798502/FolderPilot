[CmdletBinding()]
param(
    [string]$PythonExe = 'D:\python\python.exe',
    [string]$InnoCompiler = ''
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python not found: $PythonExe"
}

& $PythonExe (Join-Path $project 'tools\build_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name FolderPilot `
    --icon (Join-Path $project 'assets\FolderPilot.ico') `
    --add-data "$(Join-Path $project 'assets\FolderPilot.ico');assets" `
    --distpath (Join-Path $project 'dist') `
    --workpath (Join-Path $project 'build') `
    --specpath $project `
    (Join-Path $project 'folder_pilot.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

if (-not $InnoCompiler) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        'C:\Program Files\Inno Setup 7\ISCC.exe',
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    $InnoCompiler = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
    throw 'Inno Setup compiler was not found.'
}

& $InnoCompiler (Join-Path $project 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }

Write-Output (Join-Path $project 'release\FolderPilot-Setup-2.0.1.exe')
