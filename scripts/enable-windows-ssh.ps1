#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Enable key-based SSH access to a Windows monitoring host from its LAN.
.DESCRIPTION
    Run once on the target laptop, in an elevated PowerShell window.
    Installs the Windows OpenSSH Server feature, enables automatic startup,
    preserves existing administrator keys, and restricts the SSH firewall rule
    to the local subnet by default. Does not change power settings or start bots.
.EXAMPLE
    .\enable-windows-ssh.ps1 -AuthorizedKey 'ssh-ed25519 AAAA... workstation'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('\Assh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?\z')]
    [string]$AuthorizedKey,

    [string]$RemoteAddress = 'LocalSubnet'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'Run this script on the target Windows laptop.'
}

# Validate the key before making any system changes.
$keyParts = $AuthorizedKey.Split(' ', 3)
$null = [Convert]::FromBase64String($keyParts[1])

$capabilityName = 'OpenSSH.Server~~~~0.0.1.0'
$capability = Get-WindowsCapability -Online -Name $capabilityName
if ($capability.State -ne 'Installed') {
    $installation = Add-WindowsCapability -Online -Name $capabilityName
    if ($installation.RestartNeeded) {
        Write-Warning 'Windows reports that a restart is required to finish installing OpenSSH.'
    }
}

Set-Service -Name sshd -StartupType Automatic

$ruleName = 'OpenSSH-Server-In-TCP'
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name $ruleName -Enabled True -Profile Any -RemoteAddress $RemoteAddress
} else {
    $null = New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 `
        -Profile Any -RemoteAddress $RemoteAddress
}

# The default Windows OpenSSH configuration uses this file for administrators.
$sshDirectory = Join-Path $env:ProgramData 'ssh'
$null = New-Item -ItemType Directory -Path $sshDirectory -Force
$keysPath = Join-Path $sshDirectory 'administrators_authorized_keys'
$existingKeys = @()
if (Test-Path -LiteralPath $keysPath) {
    $existingKeys = [IO.File]::ReadAllLines($keysPath)
}
$keyIdentity = "$($keyParts[0]) $($keyParts[1])"
$alreadyPresent = @($existingKeys | Where-Object {
    $_.Trim() -eq $keyIdentity -or $_.Trim().StartsWith("$keyIdentity ")
}).Count -gt 0
if (-not $alreadyPresent) {
    $separator = if ((Test-Path -LiteralPath $keysPath) -and (Get-Item -LiteralPath $keysPath).Length -gt 0) {
        [Environment]::NewLine
    } else {
        ''
    }
    [IO.File]::AppendAllText(
        $keysPath, $separator + $AuthorizedKey.Trim() + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

# SIDs work on both English and localized Windows installations.
$administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$keyAcl = [Security.AccessControl.FileSecurity]::new()
$keyAcl.SetOwner($administrators)
$keyAcl.SetAccessRuleProtection($true, $false)
foreach ($principal in @($administrators, $system)) {
    $keyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $principal, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    ))
}
Set-Acl -LiteralPath $keysPath -AclObject $keyAcl
Start-Service -Name sshd

Write-Output "Computer: $env:COMPUTERNAME"
Write-Output "SSH login: $env:USERNAME"
Write-Output "SSH firewall scope: $RemoteAddress"
Get-Service -Name sshd | Select-Object Name, Status, StartType
Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'
} | Select-Object InterfaceAlias, IPAddress
$sshKeygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
$hostPublicKey = Join-Path $sshDirectory 'ssh_host_ed25519_key.pub'
if ((Test-Path -LiteralPath $sshKeygen) -and (Test-Path -LiteralPath $hostPublicKey)) {
    & $sshKeygen -lf $hostPublicKey
}
