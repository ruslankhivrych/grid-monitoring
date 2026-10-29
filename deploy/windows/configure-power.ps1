#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$BackupDirectory = 'C:\Monitoring\backups\host')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$null = New-Item -ItemType Directory -Path $BackupDirectory -Force
$backup = Join-Path $BackupDirectory ('power-plan-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.pow')
& powercfg.exe /export $backup SCHEME_CURRENT
if ($LASTEXITCODE -ne 0) { throw 'Could not back up the current power plan.' }
foreach ($mode in @('ac', 'dc')) {
    foreach ($setting in @('standby-timeout', 'hibernate-timeout')) {
        & powercfg.exe /change "$setting-$mode" 0
        if ($LASTEXITCODE -ne 0) { throw "Could not update $setting-$mode" }
    }
}
# If Windows exposes a lid switch, allow the monitors to run with the lid shut.
$lid = Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceName -like '*PNP0C0D*' }
$enclosure = Get-CimInstance -ClassName Win32_SystemEnclosure
$portable = @($enclosure.ChassisTypes | Where-Object { $_ -in @(8, 9, 10, 14, 30, 31, 32) }).Count -gt 0
if ($portable -or $lid) {
    & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure the AC lid action.' }
    & powercfg.exe /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure the battery lid action.' }
}
& powercfg.exe /setactive SCHEME_CURRENT
if ($LASTEXITCODE -ne 0) { throw 'Could not activate the updated power plan.' }
Write-Output "Power plan backed up to $backup"
Write-Output 'Idle sleep and idle hibernation disabled on AC and battery.'
Write-Output 'Critical battery actions and manual shutdown remain available.'
