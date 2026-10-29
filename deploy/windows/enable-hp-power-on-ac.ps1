#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$BackupDirectory = 'C:\Monitoring\backups\host')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$namespace = 'root\HP\InstrumentedBIOS'
$name = 'Power-On when AC is detected'
$setting = Get-CimInstance -Namespace $namespace -ClassName HP_BIOSEnumeration -Filter "Name='$name'"
if (-not $setting) { throw 'This HP firmware does not expose the AC power-on setting.' }
if ($setting.CurrentValue -eq 'Enable') { Write-Output 'HP automatic power-on when AC returns is already enabled.'; return }
if ($setting.IsReadOnly -or $setting.RequiresPhysicalPresence) {
    throw 'Change this setting from the laptop BIOS setup screen.'
}
$password = Get-CimInstance -Namespace $namespace -ClassName HP_BIOSPassword |
    Where-Object { $_.Name -eq 'Setup Password' }
if (-not $password -or $password.IsSet -ne 0) {
    throw 'BIOS setup is password protected or its status could not be verified; use the BIOS setup screen.'
}
$null = New-Item -ItemType Directory -Path $BackupDirectory -Force
$backup = Join-Path $BackupDirectory ('hp-ac-power-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.json')
[pscustomobject]@{Name=$name;PreviousValue=$setting.CurrentValue;TargetValue='Enable'} |
    ConvertTo-Json | Set-Content -LiteralPath $backup -Encoding UTF8
$interface = Get-CimInstance -Namespace $namespace -ClassName HP_BIOSSettingInterface
$result = Invoke-CimMethod -InputObject $interface -MethodName SetBIOSSetting `
    -Arguments @{Name=$name;Value='Enable';Password='<utf-16/>'}
if ($result.Return -ne 0) { throw "HP firmware rejected the change (code $($result.Return)); use the BIOS setup screen." }
$verified = Get-CimInstance -Namespace $namespace -ClassName HP_BIOSEnumeration -Filter "Name='$name'"
if ($verified.CurrentValue -ne 'Enable') { throw 'The firmware setting could not be verified after changing it.' }
Write-Output "HP automatic power-on when AC returns: $($verified.CurrentValue)"
Write-Output "Previous setting saved to $backup"
