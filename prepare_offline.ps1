param([switch]$OpenVendorPages)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force "offline\wheels", "vendor_installers" | Out-Null

$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Ejecuta primero .\install_lab.ps1 en este ordenador." }
& $python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))"
if ($LASTEXITCODE -ne 0) {
    throw "El kit offline esta fijado a Python 3.13 x64."
}

& $python -m pip download --only-binary=:all: --dest "offline\wheels" -r requirements-hardware.txt
if ($LASTEXITCODE -ne 0) { throw "No se han podido descargar todas las ruedas." }

$downloads = @(
    @{
        Url = "https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe"
        Path = "vendor_installers\python-3.13.14-amd64.exe"
        Sha256 = "c54d9b9bbb8a36e6489363ddd01139707fd781d72f1f9e90c7ec65d0061368e0"
    },
    @{
        Url = "https://www.navitar.com/-/media/project/oneweb/oneweb/navitar/pixelink-windows-sdk-release-13/pixelink_software.exe"
        Path = "vendor_installers\pixelink_software.exe"
        Sha256 = "5f9d6075c1b362b4210e41fbf845292344ba79cde582cc35ddc1d5350ab99996"
    },
    @{
        Url = "https://media.thorlabs.com/contentassets/98b8893ed3ff41cc8b1794e39e81e6fe/thorlabs_kinesis_setup_26708_x64.exe?v=0325125008"
        Path = "vendor_installers\Thorlabs_Kinesis_1.14.59_x64.exe"
        Sha256 = "12240a38699d2fa9a0974daccb52a0f66867b394963e52fcc964f2d42ba6b88e"
    }
)
foreach ($item in $downloads) {
    if (-not (Test-Path $item.Path)) {
        Write-Host "Descargando $(Split-Path $item.Path -Leaf)..."
        Invoke-WebRequest -UseBasicParsing $item.Url -OutFile $item.Path
    }
    if ($item.Sha256 -and (Get-FileHash $item.Path -Algorithm SHA256).Hash -ne $item.Sha256) {
        throw "Checksum incorrecto: $($item.Path)"
    }
}

Get-ChildItem "offline\wheels\*.whl", "vendor_installers\*.exe" |
    Sort-Object Name |
    ForEach-Object { "$(Get-FileHash $_.FullName -Algorithm SHA256 | Select-Object -ExpandProperty Hash)  $($_.Name)" } |
    Set-Content "offline\SHA256SUMS.txt"

if ($OpenVendorPages) {
    Start-Process "https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=Motion_Control"
    Start-Process "https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html"
}

Write-Host "Kit Python, Pixelink y Kinesis listo."
Write-Host "NI-VISA es opcional; pyvisa-py cubre serie, USB y red."
