# cansim: pobiera oficjalne repozytoria STMicroelectronics/cmsis_device_h7 i cmsis_device_f4 jako ZIP z GitHub
# i rozpakowuje do third_party\cmsis_device_h7 oraz third_party\cmsis_device_f4.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # bez paska postepu Invoke-WebRequest pobiera duzo szybciej
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'third_party'
New-Item -ItemType Directory -Force -Path $dest | Out-Null

foreach ($fam in 'h7', 'f4') {
    $name = "cmsis_device_$fam"
    $target = Join-Path $dest $name
    if (Test-Path (Join-Path $target 'Include')) {
        Write-Host "$name juz jest, pomijam"
        continue
    }
    $ok = $false
    foreach ($branch in 'master', 'main') {
        $url = "https://github.com/STMicroelectronics/$name/archive/refs/heads/$branch.zip"
        $zip = Join-Path $env:TEMP "$name.zip"
        $tmp = Join-Path $env:TEMP "cansim_$name"
        try {
            Write-Host "Pobieram $url ..."
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        } catch {
            Write-Host "  nie udalo sie: $($_.Exception.Message)"
            continue
        }
        Write-Host "Rozpakowuje (to moze potrwac minute) ..."
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }
        Move-Item $inner.FullName $target
        Remove-Item -Recurse -Force $tmp
        Remove-Item -Force $zip
        $ok = $true
        break
    }
    if (-not $ok) {
        Write-Host "BLAD: nie udalo sie pobrac $name. Sprawdz internet albo pobierz recznie (README)." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path (Join-Path $target 'Include'))) {
        Write-Host "BLAD: w $target nie ma folderu Include." -ForegroundColor Red
        exit 1
    }
    Write-Host "OK: $target" -ForegroundColor Green
}
Write-Host ""
Write-Host "Gotowe. Mozesz uruchomic start.bat" -ForegroundColor Green
