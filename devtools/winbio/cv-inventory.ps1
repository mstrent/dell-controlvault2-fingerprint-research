# cv-inventory.ps1 - read-only inventory of Dell ControlVault components on
# Windows: services, registry settings, biometric database files, files
# recently written by the ControlVault services, and trace providers.
# Changes nothing. Run from an administrator PowerShell (some keys and folders
# need it):
#   powershell -ExecutionPolicy Bypass -File D:\cv-inventory.ps1 > $env:USERPROFILE\Desktop\cv-inventory.txt
param([int]$Hours = 24)
$ErrorActionPreference = 'SilentlyContinue'
$since = (Get-Date).AddHours(-$Hours)
function H($t) { "`n===== $t" }

H 'Services'
Get-CimInstance Win32_Service |
    Where-Object { $_.Name -match 'cv|ush|bcm|hoststorage|hostcontrol|wbio' -or $_.PathName -match 'bcm|cvusb|ush' } |
    Format-Table -AutoSize Name, State, StartMode, PathName | Out-String -Width 250

H 'Service registry keys (parameters may name storage paths)'
foreach ($s in 'hoststoragesvc', 'hostcontrolsvc', 'ushupgradesvc', 'cvusbdrv', 'WbioSrvc') {
    $k = "HKLM:\SYSTEM\CurrentControlSet\Services\$s"
    if (Test-Path $k) {
        "--- $k"
        Get-ChildItem -Recurse $k | ForEach-Object { "  [$($_.Name)]"; $_ | Get-ItemProperty | Out-String -Width 250 }
        Get-ItemProperty $k | Out-String -Width 250
    }
}

H 'Broadcom / Dell ControlVault registry keys'
foreach ($k in 'HKLM:\SOFTWARE\Broadcom', 'HKLM:\SOFTWARE\WOW6432Node\Broadcom', 'HKLM:\SOFTWARE\Dell\ControlVault',
               'HKLM:\SOFTWARE\Wow6432Node\Dell\ControlVault') {
    if (Test-Path $k) {
        "--- $k"
        Get-ChildItem -Recurse $k | ForEach-Object { "  [$($_.Name)]"; $_ | Get-ItemProperty | Out-String -Width 250 }
    }
}

H 'Biometric database files'
Get-ChildItem C:\Windows\System32\WinBioDatabase -Force | Format-Table -AutoSize Name, Length, LastWriteTime | Out-String -Width 250

H "Files written in the last $Hours h with ControlVault-related names or folders"
$roots = 'C:\ProgramData', 'C:\Windows\System32', 'C:\Windows\SysWOW64', 'C:\Windows\Temp', 'C:\Program Files', 'C:\Program Files (x86)'
foreach ($r in $roots) {
    Get-ChildItem $r -Recurse -File -Force |
        Where-Object { $_.LastWriteTime -gt $since -and $_.FullName -match 'bcm|broadcom|cvault|credential ?vault|controlvault|ush|cv_|\\cv\\|winbio' } |
        Format-Table -AutoSize FullName, Length, LastWriteTime | Out-String -Width 250
}

H 'Broadcom / ControlVault program folders'
foreach ($d in 'C:\Program Files\Broadcom Corporation', 'C:\Program Files\Broadcom', 'C:\Program Files (x86)\Broadcom',
               'C:\ProgramData\Broadcom', 'C:\ProgramData\Broadcom Corporation') {
    if (Test-Path $d) { "--- $d"; Get-ChildItem -Recurse -Force $d | Format-Table -AutoSize FullName, Length, LastWriteTime | Out-String -Width 250 }
}

H 'Trace (ETW) providers with ControlVault-related names'
logman query providers | Select-String -Pattern 'broadcom|bcm|cvault|controlvault|ush|wbf|biometric'

H 'Event logs with ControlVault-related names'
Get-WinEvent -ListLog * | Where-Object { $_.LogName -match 'broadcom|bcm|cvault|controlvault|ush|biometric' } |
    Format-Table -AutoSize LogName, RecordCount, LastWriteTime | Out-String -Width 250

"`nDone $(Get-Date -Format s)"
