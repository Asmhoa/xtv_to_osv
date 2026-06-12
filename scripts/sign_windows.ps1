param(
    [string]$Executable = "dist\XtraToOsmo.exe"
)

$ErrorActionPreference = "Stop"
$certificatePath = Join-Path $env:RUNNER_TEMP "xtra-to-osmo-signing.pfx"
$certificateBytes = [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE)
[IO.File]::WriteAllBytes($certificatePath, $certificateBytes)

try {
    & signtool.exe sign `
        /fd SHA256 `
        /td SHA256 `
        /tr "http://timestamp.digicert.com" `
        /f $certificatePath `
        /p $env:WINDOWS_CERTIFICATE_PASSWORD `
        $Executable
    & signtool.exe verify /pa /v $Executable
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $certificatePath
}
