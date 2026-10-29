#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Root = 'C:\Monitoring',
    [ValidatePattern('\A[a-z][a-z0-9-]+\z')]
    [string]$Name = 'grid-monitoring',
    [switch]$PrepareOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$project = Join-Path $Root "apps\$Name"
$data = Join-Path $Root "data\$Name"
$logs = Join-Path $Root "logs\$Name"
$backups = Join-Path $Root "backups\$Name"
$python = Join-Path $project '.venv\Scripts\python.exe'
$launcher = Join-Path $project 'deploy\windows\run_bot.py'
$backupScript = Join-Path $project 'deploy\windows\backup_db.py'
$serviceSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-19')
$adminSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')

foreach ($directory in @($project, $data, $logs, $backups)) {
    $null = New-Item -ItemType Directory -Path $directory -Force
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($adminSid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($principal in @($adminSid, $systemSid)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
        ))
    }
    $serviceRights = if ($directory -eq $project) { 'ReadAndExecute' } else { 'Modify' }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $serviceSid, $serviceRights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    ))
    Set-Acl -LiteralPath $directory -AclObject $acl
}
if ($PrepareOnly) {
    Write-Output 'Directories and service-account permissions prepared.'
    return
}

foreach ($file in @($python, $launcher, $backupScript, (Join-Path $project 'bot\.env'))) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Required file is missing: $file" }
}
foreach ($taskName in @($Name, "$Name-backup")) {
    $existing = Get-ScheduledTask -TaskPath '\Monitoring\' -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.State -eq 'Running') { throw "Stop the running task before updating: $taskName" }
}
& $python $launcher --project $project --data $data --logs $logs --check
if ($LASTEXITCODE -ne 0) { throw 'Bot configuration validation failed.' }

$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
try { $null = $scheduler.GetFolder('\Monitoring') }
catch { $null = $scheduler.GetFolder('\').CreateFolder('Monitoring') }
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-19' -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $project `
    -Argument ('-u "{0}" --project "{1}" --data "{2}" --logs "{3}"' -f $launcher, $project, $data, $logs)
$startup = New-ScheduledTaskTrigger -AtStartup
$watchdog = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$null = Register-ScheduledTask -TaskPath '\Monitoring\' -TaskName $Name -Action $action `
    -Trigger @($startup, $watchdog) -Settings $settings -Principal $principal `
    -Description 'FSolar Telegram monitoring; boot startup and automatic recovery without interactive logon.' -Force

$backupAction = New-ScheduledTaskAction -Execute $python -WorkingDirectory $project `
    -Argument ('"{0}" --database "{1}" --directory "{2}" --keep 14' -f $backupScript, (Join-Path $data 'bot.db'), $backups)
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
$null = Register-ScheduledTask -TaskPath '\Monitoring\' -TaskName "$Name-backup" `
    -Action $backupAction -Trigger (New-ScheduledTaskTrigger -Daily -At '03:15') `
    -Settings $backupSettings -Principal $principal -Description 'Daily SQLite backups; retain 14 automatic copies.' -Force
Start-ScheduledTask -TaskPath '\Monitoring\' -TaskName $Name
Write-Output "Registered \Monitoring\$Name and \Monitoring\$Name-backup."
