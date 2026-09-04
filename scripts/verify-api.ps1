#requires -Version 7.0
<#
.SYNOPSIS
    Перевірка живого FSolar API, який використовує Telegram-бот.
.DESCRIPTION
    RSA-шифрує пароль публічним ключем FSolar,
    логіниться (/userlogin), тягне знімок (get_device_snapshot) і журнал тривог
    (device_warring_list), і друкує поля, які бот мапить у стан мережі/батареї.
    Запустіть під час реального відключення, щоб підтвердити grid-OFF значення.
.EXAMPLE
    pwsh ./scripts/verify-api.ps1 -UserName you@example.com -DeviceSn YOUR_SERIAL
.NOTES
    Потрібен PowerShell 7+ (.NET Core: ImportSubjectPublicKeyInfo + RSA PKCS#1).
    Пароль вводиться безпечно (не в аргументах); токен не друкується.
#>
param(
    [Parameter(Mandatory)] [string] $UserName,
    [Parameter(Mandatory)] [string] $DeviceSn,
    [string] $DeviceType = "OG",
    [int]    $Days = 7
)

$ErrorActionPreference = "Stop"
$BaseUrl = "https://shine-api.felicitysolar.com"

# Публічний RSA-ключ FSolar (SPKI, base64), отриманий з веб-клієнта FSolar.
$PublicKeySpki = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnAJE68pjWZmtSg6ZJs9FZugJXC6bBSluTW6mJttOLOaljrdErVnM5DNN+YFzpB9pAysTErjY1bnSVuEwQSwptnqUji7Ch2qMj2n+0eCp8p6vtSh7/tFr2ul8nDRtkoswLANAIwtUk/G85ipMpmY1W642LImnEJmGkkddlbjbjxJTZWR5hc/d9cPWb+AR77LxFFrMik3c+44v1kQlIPFP6EjIbOvt/Lv7fHWD9JI/YzN4y1gK7C/VQdNGuikQyNg+5W3rg9ecYf9I5uLAQwY/hxeI3lbNsErebqKe2EbJ8AwcNIC0lDBz53Sq0ML89QapEuy3fB+upuctxLULVDCbNwIDAQAB"

function Encrypt-Password([string] $plain) {
    $rsa = [System.Security.Cryptography.RSA]::Create()
    $read = 0
    $rsa.ImportSubjectPublicKeyInfo([Convert]::FromBase64String($PublicKeySpki), [ref] $read) | Out-Null
    $cipher = $rsa.Encrypt([Text.Encoding]::UTF8.GetBytes($plain),
                           [System.Security.Cryptography.RSAEncryptionPadding]::Pkcs1)
    return [Convert]::ToBase64String($cipher)
}

function Invoke-FSolar([string] $path, $bodyObj, $token) {
    $headers = @{ "lang" = "en_US"; "source" = "WEB" }
    if ($token) { $headers["Authorization"] = $token }
    $json = $bodyObj | ConvertTo-Json -Compress
    $resp = Invoke-RestMethod -Uri "$BaseUrl$path" -Method Post -Body $json `
                              -ContentType "application/json" -Headers $headers
    if ($resp.code -ne 200) { throw "API code=$($resp.code): $($resp.message)" }
    return $resp.data
}

# --- 1. Логін ---
$secure = Read-Host "Пароль FSolar" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $secure).Password
Write-Host "→ Логін ($UserName)..." -ForegroundColor Cyan
$loginData = Invoke-FSolar "/userlogin" @{ userName = $UserName; password = (Encrypt-Password $plain); version = "1.0" } $null
$token = $loginData.token
if (-not $token) { throw "Не отримано токен" }
Write-Host "✓ Логін успішний (токен отримано)" -ForegroundColor Green

# --- 2. Знімок (мережа + батарея) ---
Write-Host "`n→ get_device_snapshot ($DeviceSn)..." -ForegroundColor Cyan
$dateStr = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
$d = Invoke-FSolar "/device/get_device_snapshot" @{ deviceSn = $DeviceSn; deviceType = $DeviceType; dateStr = $dateStr } $token

$volt = if ($d.acRInVolt) { [double]$d.acRInVolt } else { 0 }
$isPresent = ($volt -gt 50) -or ($d.workModeStr -like "*Line*")
$soc = if ($d.emsSoc) { $d.emsSoc } elseif ($d.emsSocAvg) { $d.emsSocAvg } else { $d.battSoc }

Write-Host "  Мережа:    напруга=$($d.acRInVolt) В, частота=$($d.acRInFreq) Гц, режим='$($d.workModeStr)'"
Write-Host ("  isPresent: {0}" -f $(if ($isPresent) { "МЕРЕЖА Є ✓" } else { "МЕРЕЖІ НЕМАЄ ✗" })) `
           -ForegroundColor $(if ($isPresent) { "Green" } else { "Red" })
Write-Host "  Батарея:   SOC=$soc %, напруга=$($d.emsVoltage) В, струм=$($d.emsCurrent) А"
Write-Host "  Споживання: зараз=$($d.totalConsumPower) Вт, сьогодні=$($d.eLoadToday) кВт·год, місяць=$($d.eLoadMonth) кВт·год"
Write-Host "  Оновлено:  $($d.dataTimeStr)  (reportFreq=$($d.reportFreq)s)"

# --- 3. Тривоги (зникнення мережі) ---
Write-Host "`n→ device_warring_list (останні $Days дн.)..." -ForegroundColor Cyan
$now  = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$from = [DateTimeOffset]::UtcNow.AddDays(-$Days).ToUnixTimeMilliseconds()
$al = Invoke-FSolar "/device/device_warring_list" @{
    pageNum = 1; pageSize = 50; plantName = ""; deviceSn = $DeviceSn; status = ""
    warringType = ""; userName = ""; orgCode = ""; faultcode = ""; deviceModel = ""
    deviceAlias = ""; leftDate = $from; rightDate = $now
} $token

$mains = @($al.dataList | Where-Object { $_.warnCode -eq "4" })
Write-Host "  Усього тривог: $($al.dataList.Count); зникнень мережі (warnCode 4): $($mains.Count)"
$mains | Select-Object -First 10 | ForEach-Object {
    Write-Host "    • $($_.dataTimeStr)  $($_.warringName)"
}

Write-Host "`n✓ Готово. Якщо запускали під час відключення — порівняйте acRInVolt/workModeStr вище." -ForegroundColor Green
Write-Host "  Не забудьте змінити пароль FSolar після налагодження." -ForegroundColor Yellow
