
# Diagnose-EP6Restore.ps1
# Read-only diagnostics for the EP6 Data Protector restore failure.
# Does NOT modify the Data Protector catalog, mount tapes, restart services,
# delete files, or change SAP/Oracle configuration.

$ErrorActionPreference = "Continue"

$OutRoot = "C:\Temp\DP_EP6_Restore_Diagnostics"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutDir = Join-Path $OutRoot $Stamp
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$Report = Join-Path $OutDir "Diagnostic_Report.txt"
Start-Transcript -Path $Report -Force | Out-Null

function Section($Name) {
    Write-Host "`n========== $Name ==========" -ForegroundColor Cyan
}

function Save-CommandHelp($Exe, $Args, $FileName) {
    if (Test-Path $Exe) {
        "COMMAND: $Exe $Args" |
            Out-File (Join-Path $OutDir $FileName) -Encoding utf8
        try {
            $result = & $Exe $Args 2>&1
            $result | Out-File (Join-Path $OutDir $FileName) `
                -Append -Encoding utf8
        } catch {
            $_ | Out-File (Join-Path $OutDir $FileName) `
                -Append -Encoding utf8
        }
    }
}

Section "1. Host and time"
Get-Date
hostname
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, LastBootUpTime
whoami

Section "2. Data Protector installation paths"
$PossibleRoots = @(
    "D:\Program Files\OmniBack",
    "C:\Program Files\OmniBack",
    "D:\ProgramData\OmniBack",
    "C:\ProgramData\OmniBack"
)

$ExistingRoots = $PossibleRoots |
    Where-Object { Test-Path $_ }

$ExistingRoots | ForEach-Object { Write-Host $_ }

$BinDirs = @(
    "D:\Program Files\OmniBack\bin",
    "C:\Program Files\OmniBack\bin"
) | Where-Object { Test-Path $_ }

Section "3. Locate diagnostic and media-management utilities"
$ToolNames = @(
    "omnidbutil.exe",
    "omniminit.exe",
    "omniinfo.exe",
    "omnir.exe",
    "omniusers.exe",
    "omniinetpasswd.exe",
    "omnicheck.exe",
    "omnisv.exe"
)

$Tools = foreach ($dir in $BinDirs) {
    foreach ($name in $ToolNames) {
        $p = Join-Path $dir $name
        if (Test-Path $p) {
            Get-Item $p |
                Select-Object Name, FullName, Length, LastWriteTime
        }
    }
}
$Tools | Format-Table -AutoSize

Section "4. Data Protector services"
Get-CimInstance Win32_Service |
    Where-Object {
        $_.Name -match 'omni|DataProtector|Data Protector' -or
        $_.DisplayName -match 'Data Protector|OmniBack'
    } |
    Select-Object Name, DisplayName, State, StartMode, StartName, PathName |
    Format-List

Section "5. Locate Data Protector logs"
$LogRoots = @(
    "D:\ProgramData\OmniBack\log",
    "C:\ProgramData\OmniBack\log",
    "D:\Program Files\OmniBack\log",
    "C:\Program Files\OmniBack\log"
) | Where-Object { Test-Path $_ }

$LogFiles = foreach ($root in $LogRoots) {
    Get-ChildItem $root -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LastWriteTime -ge [datetime]"2026-10-26" -and
            $_.LastWriteTime -le [datetime]"2026-10-30"
        }
}

$LogFiles |
    Select-Object FullName, Length, LastWriteTime |
    Export-Csv (Join-Path $OutDir "Recent_DP_Logs.csv") `
        -NoTypeInformation

Section "6. Search logs for restore session and media errors"
$Patterns = @(
    'No more media for restore',
    'some medium is missing',
    'missing medium',
    'MSL4048_DRV1_LTO5',
    'ESSBKCLIB01_MSL8096_DRV1',
    'EP6\.1',
    'EP6\.3',
    'File cannot be replaced',
    'Received ABORT request',
    'media.*unavailable',
    'medium.*not found',
    'barcode',
    'medium ID',
    '2026/10/27-14'
)

$SearchResults = foreach ($file in $LogFiles) {
    Select-String -Path $file.FullName -Pattern $Patterns `
        -ErrorAction SilentlyContinue |
        Select-Object @{N="LogFile";E={$file.FullName}},
                      LineNumber, Line
}

$SearchResults |
    Export-Csv (Join-Path $OutDir "Restore_Error_Matches.csv") `
        -NoTypeInformation

$SearchResults |
    Format-Table LogFile, LineNumber, Line -Wrap

Section "7. Find Data Protector media/device configuration references"
$ConfigRoots = @(
    "D:\ProgramData\OmniBack\Config",
    "C:\ProgramData\OmniBack\Config"
) | Where-Object { Test-Path $_ }

$ConfigFiles = foreach ($root in $ConfigRoots) {
    Get-ChildItem $root -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Extension -in @(".conf", ".cfg", ".ini", ".txt") -or
            $_.Name -match 'device|media|library|pool'
        }
}

$ConfigMatches = foreach ($file in $ConfigFiles) {
    Select-String -Path $file.FullName `
        -Pattern 'MSL4048_DRV1_LTO5|ESSBKCLIB01_MSL8096_DRV1' `
        -ErrorAction SilentlyContinue |
        Select-Object @{N="File";E={$file.FullName}}, LineNumber, Line
}

$ConfigMatches |
    Export-Csv (Join-Path $OutDir "Device_Config_Matches.csv") `
        -NoTypeInformation
$ConfigMatches | Format-Table -Wrap

Section "8. Check restore destination and file metadata"
$Destination = "D:\EP6Restore\oracle\ep6"

if (Test-Path $Destination) {
    Get-Item $Destination |
        Select-Object FullName, Attributes, LastWriteTime

    $DestinationFiles = Get-ChildItem $Destination -File -Recurse `
        -ErrorAction SilentlyContinue

    $DestinationFiles |
        Select-Object FullName, Length, LastWriteTime, Attributes |
        Export-Csv (Join-Path $OutDir "Restore_Destination_Files.csv") `
            -NoTypeInformation

    Write-Host "Files found under destination: $($DestinationFiles.Count)"
} else {
    Write-Warning "Restore destination does not exist: $Destination"
}

Section "9. Check free space on destination volume"
Get-CimInstance Win32_LogicalDisk |
    Where-Object { $_.DeviceID -in @("C:", "D:") } |
    Select-Object DeviceID, VolumeName,
        @{N="SizeGB";E={[math]::Round($_.Size / 1GB, 2)}},
        @{N="FreeGB";E={[math]::Round($_.FreeSpace / 1GB, 2)}} |
    Format-Table -AutoSize

Section "10. Record utility help only (no media/catalog operations)"
foreach ($dir in $BinDirs) {
    Save-CommandHelp (Join-Path $dir "omnidbutil.exe") "-help" `
        "omnidbutil_help.txt"
    Save-CommandHelp (Join-Path $dir "omniinfo.exe") "-help" `
        "omniinfo_help.txt"
}

Section "11. Export recent relevant Windows events"
try {
    Get-WinEvent -FilterHashtable @{
        LogName = "System"
        StartTime = [datetime]"2026-10-27"
        EndTime = [datetime]"2026-10-29 23:59:59"
    } -ErrorAction Stop |
        Where-Object {
            $_.ProviderName -match 'Tape|Storport|Disk|Ntfs|Service Control Manager' -or
            $_.Message -match 'tape|medium changer|library|SCSI|device'
        } |
        Select-Object TimeCreated, ProviderName, Id, LevelDisplayName, Message |
        Export-Csv (Join-Path $OutDir "Relevant_System_Events.csv") `
            -NoTypeInformation
} catch {
    Write-Warning "Could not read System events: $($_.Exception.Message)"
}

Section "12. Summary"
Write-Host "Output folder: $OutDir"
Write-Host "Main report:   $Report"
Write-Host "Review these files:"
Write-Host "  Restore_Error_Matches.csv"
Write-Host "  Device_Config_Matches.csv"
Write-Host "  Recent_DP_Logs.csv"
Write-Host "  Relevant_System_Events.csv"
Write-Host ""
Write-Host "No restore, tape mount, catalog update, or service restart was requested."

Stop-Transcript | Out-Null
Write-Host "`nDiagnostics completed. Output: $OutDir" -ForegroundColor Green
