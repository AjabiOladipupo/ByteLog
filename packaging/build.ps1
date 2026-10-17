<#
.SYNOPSIS
  Build ByteLog for Windows: dist\ByteLog\ (both exes), a zip, and an installer if Inno
  Setup 6 is installed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipSync -SkipInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipSync,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
            (Join-Path $env:APPDATA 'Python\Python314\Scripts\uv.exe'),
            (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
            (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw 'uv not found. Install it from https://docs.astral.sh/uv/ and re-run.'
}

function Find-Iscc {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

$uv = Find-Uv
$init = Get-Content (Join-Path $Root 'src\bytelog\__init__.py') -Raw
if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw 'Cannot read __version__' }
$Version = $Matches[1]
Write-Host "Building ByteLog $Version"

if (-not $SkipSync) {
    Invoke-Checked $uv @('sync', '--group', 'build')
}

$Dist = Join-Path $Root 'dist'
$Work = Join-Path $Root 'build'
Invoke-Checked $uv @('run', 'pyinstaller', '--noconfirm', '--clean',
    '--distpath', $Dist, '--workpath', $Work, (Join-Path $Root 'packaging\bytelog.spec'))

$AppDir = Join-Path $Dist 'ByteLog'
foreach ($exe in @('ByteLog.exe', 'ByteLogCollector.exe')) {
    if (-not (Test-Path (Join-Path $AppDir $exe))) { throw "Build did not produce $exe" }
}

# .NET ZipFile instead of Compress-Archive: faster, and retried because antivirus often
# holds freshly written files open for a few seconds ("used by another process").
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = Join-Path $Dist "ByteLog-$Version-win64.zip"
for ($attempt = 1; ; $attempt++) {
    if (Test-Path $Zip) { Remove-Item $Zip -Force }
    try {
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $AppDir, $Zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
        break
    } catch [System.IO.IOException] {
        if ($attempt -ge 5) { throw }
        Write-Host "Zip attempt $attempt failed ($($_.Exception.Message)); retrying"
        Start-Sleep -Seconds 5
    }
}
Write-Host "Zip: $Zip"

if ($SkipInstaller) { return }
$iscc = Find-Iscc
if ($iscc) {
    # Retried for the same reason as the zip: a scanner may still hold the previous setup.exe.
    for ($attempt = 1; ; $attempt++) {
        & $iscc /Q "/DAppVersion=$Version" "/DBuildDir=$AppDir" "/O$Dist" (Join-Path $Root 'packaging\bytelog.iss')
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -ge 3) { throw "$iscc failed with exit code $LASTEXITCODE" }
        Write-Host "Installer attempt $attempt failed; retrying"
        Start-Sleep -Seconds 5
    }
    Write-Host "Installer: $(Join-Path $Dist "ByteLog-$Version-setup.exe")"
} else {
    Write-Host 'Inno Setup 6 (ISCC.exe) not found; skipped the installer.'
    Write-Host 'Install it with: winget install JRSoftware.InnoSetup'
}
