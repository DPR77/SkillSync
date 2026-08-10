<#
    Downloads the official embeddable Python from python.org into a private folder.

    This is the last-resort path used by launch.cmd when neither an existing Python nor
    winget is available. It needs no administrator rights and touches nothing outside
    -Dest. The build is pinned by version *and* SHA-256: the hashes below are the only
    thing this script will accept, so a compromised mirror or a redirected download fails
    closed instead of executing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Dest
)

$ErrorActionPreference = 'Stop'

$Version = '3.12.10'
$Sha256 = @{
    'amd64' = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
    'arm64' = '3065efc3d382d1cda66757ac71ade11904fa6e350f5a97eb74811acd71ba5532'
}

function Fail($msg) {
    Write-Host "  [skill-sync] $msg" -ForegroundColor Red
    exit 1
}

switch ($env:PROCESSOR_ARCHITECTURE) {
    'ARM64' { $arch = 'arm64' }
    'AMD64' { $arch = 'amd64' }
    default { Fail "unsupported processor architecture '$env:PROCESSOR_ARCHITECTURE' - install Python from https://www.python.org/downloads/" }
}

$url = "https://www.python.org/ftp/python/$Version/python-$Version-embed-$arch.zip"
$expected = $Sha256[$arch]

if ($url -notlike 'https://www.python.org/*') { Fail 'refusing to download from a host other than python.org' }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "skill-sync-python-$([guid]::NewGuid().ToString('N')).zip"

try {
    Write-Host "  [skill-sync] downloading Python $Version ($arch) from python.org"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 300

    $actual = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Fail "checksum mismatch - expected $expected, got $actual. Nothing was installed."
    }
    Write-Host "  [skill-sync] checksum verified"

    if (Test-Path $Dest) { Remove-Item -Path $Dest -Recurse -Force }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($tmp, $Dest)

    $exe = Join-Path $Dest 'python.exe'
    if (-not (Test-Path $exe)) { Fail "the archive did not contain python.exe" }

    Write-Host "  [skill-sync] Python installed at $exe"
    exit 0
}
catch {
    Fail "download failed: $($_.Exception.Message)"
}
finally {
    if (Test-Path $tmp) { Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue }
}
