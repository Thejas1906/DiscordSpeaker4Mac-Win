# Build the Windows x64 app folder and (if Inno Setup is installed) an installer.
# Run in PowerShell from the repo root with Python 3.12 x64:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
$Version = if ($env:DS_VERSION) { $env:DS_VERSION } else { "2.0.0" }
python -m pip install -r src\requirements-build.txt
# librespot (Spotify Connect receiver, MIT). No official Windows binaries: build with Rust (cargo) or point DS_LIBRESPOT at one.
if (-not $env:DS_LIBRESPOT) {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw "Install Rust (https://rustup.rs) or set DS_LIBRESPOT to librespot.exe" }
    cargo install librespot --locked --version 0.8.0
    $env:DS_LIBRESPOT = "$env:USERPROFILE\.cargo\bin\librespot.exe"
}
New-Item -ItemType Directory -Force build | Out-Null
# .ico from the PNG logo via Qt (no extra dependency)
python -c "from PySide6.QtGui import QImage; QImage('src/ui/assets/logo.png').scaled(256,256).save('build/DiscordSpeaker.ico')"
$env:DS_VERSION = $Version
python -m PyInstaller packaging\discordspeaker.spec --noconfirm --distpath dist --workpath build\pyi
& dist\DiscordSpeaker\DiscordSpeaker.exe --selftest
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($iscc) { & iscc /DAppVersion=$Version packaging\windows\DiscordSpeaker.iss; Write-Host "Installer: dist\DiscordSpeaker-$Version-windows-x64-setup.exe" }
else { Write-Host "Inno Setup (iscc) not found: portable folder is at dist\DiscordSpeaker\. Install Inno Setup 6 to build the installer." }
