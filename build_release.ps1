param([string]$Version)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run .\install_lab.ps1 -Hardware first." }
if (-not $Version) { $Version = & $python -c "import pumpauto; print(pumpauto.__version__)" }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must use MAJOR.MINOR.PATCH." }
if (Get-Process PCMWriter -ErrorAction SilentlyContinue) {
    throw "Close every running PCMWriter.exe before building a release."
}

& $python -c "import struct; raise SystemExit(struct.calcsize('P') != 8)"
if ($LASTEXITCODE -ne 0) { throw "PCMWriter releases require 64-bit Python." }
& $python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install the build dependency." }

& $python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed; release build stopped." }
& $python -m pumpauto self-test
if ($LASTEXITCODE -ne 0) { throw "Self-test failed; release build stopped." }

$workPath = Join-Path $env:TEMP "PCMWriter-build-$PID"
$distPath = Join-Path $env:TEMP "PCMWriter-dist-$PID"
& $python -m PyInstaller --clean --noconfirm --workpath $workPath --distpath $distPath PCMWriter.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$bundle = Join-Path $distPath "PCMWriter"
Copy-Item -LiteralPath "config.example.json" -Destination "$bundle\config.json"
Copy-Item -LiteralPath "config.example.json" -Destination "$bundle\config.example.json"
Copy-Item -LiteralPath "README.md", "LICENSE", "LAB_SETUP.md", "EQUIPMENT.md" -Destination $bundle

& "$bundle\PCMWriter.exe" --smoke-test
if ($LASTEXITCODE -ne 0) { throw "The packaged executable failed its startup smoke test." }

New-Item -ItemType Directory -Force -Path "release" | Out-Null
$name = "PCMWriter-Windows-x64-v$Version.zip"
$zip = Join-Path "release" $name
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        Compress-Archive -Path "$bundle\*" -DestinationPath $zip -CompressionLevel Optimal -ErrorAction Stop
        break
    } catch {
        if ($attempt -eq 5) { throw }
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
        Start-Sleep -Seconds 1
    }
}
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $name" | Set-Content -Encoding ascii "$zip.sha256"

Write-Host "Release ready: $zip"
Write-Host "SHA-256: $hash"
Write-Host "Kinesis, Pixelink and VISA drivers remain external prerequisites."
