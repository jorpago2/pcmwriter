param(
    [switch]$Hardware,
    [switch]$Offline,
    [switch]$InstallVendorDrivers,
    [switch]$OpenVendorPages
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Offline) {
    $manifest = "offline\SHA256SUMS.txt"
    if (-not (Test-Path $manifest)) { throw "Missing $manifest." }
    foreach ($line in Get-Content $manifest) {
        $expected, $name = $line -split "\s+", 2
        $file = @("offline\wheels\$name", "vendor_installers\$name") |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $file -or (Get-FileHash $file -Algorithm SHA256).Hash -ne $expected) {
            throw "Offline file is missing or has been modified: $name"
        }
    }
}

if ($InstallVendorDrivers) {
    $installers = @(
        Get-ChildItem "vendor_installers\*Kinesis*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        Get-Item "vendor_installers\pixelink_software.exe" -ErrorAction SilentlyContinue
    ) | Where-Object { $_ }
    if ($installers.Count -lt 2) {
        throw "Kinesis or Pixelink is missing from vendor_installers. Run .\prepare_offline.ps1 -OpenVendorPages first."
    }
    foreach ($installer in $installers) {
        Write-Host "Installing $($installer.Name)..."
        Start-Process $installer.FullName -Wait
    }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $havePython313 = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -c "pass" 2>$null
        $havePython313 = $LASTEXITCODE -eq 0
    }
    if ($Offline -and -not $havePython313) {
        $installer = "vendor_installers\python-3.13.14-amd64.exe"
        if (-not (Test-Path $installer)) {
            throw "Missing $installer. Run prepare_offline.ps1 on a computer with Internet access."
        }
        Start-Process $installer -ArgumentList "/quiet InstallAllUsers=0 Include_launcher=1 Include_pip=1 Include_test=0 Shortcuts=0" -Wait
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv .venv
    } elseif (Test-Path "$env:LocalAppData\Programs\Python\Python313\python.exe") {
        & "$env:LocalAppData\Programs\Python\Python313\python.exe" -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python 3.13 environment." }
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$requirements = if ($Hardware) { "requirements-hardware.txt" } else { "requirements.txt" }
if ($Offline) {
    if (-not (Test-Path "offline\wheels\*.whl")) {
        throw "No offline wheels are available. Run prepare_offline.ps1 on a computer with Internet access."
    }
    & $python -m pip install --no-index --find-links "offline\wheels" -r $requirements
} else {
    & $python -m pip install --upgrade pip
    & $python -m pip install -r $requirements
}
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies." }

if (-not (Test-Path "config.json")) {
    Copy-Item "config.example.json" "config.json"
}

& $python -m pumpauto self-test
if ($LASTEXITCODE -ne 0) { throw "The self-test failed." }
if ($Hardware) { & $python -m pumpauto diagnostics }

if ($OpenVendorPages) {
    Start-Process "https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=Motion_Control"
    Start-Process "https://www.navitar.com/products/pixelink-cameras/pixelink-sdk"
    Start-Process "https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html"
}

Write-Host "Setup complete. Run .\PCMWriter.bat"
if (-not $Hardware) {
    Write-Host "For offline hardware setup: .\install_lab.ps1 -Hardware -Offline -InstallVendorDrivers"
}
