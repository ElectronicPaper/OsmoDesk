<#
    Find the Osmo Pocket on its USB RNDIS link.

    The camera exposes a "Remote NDIS based Internet Sharing Device" in its
    default USB mode (PID_0020) but serves no DHCP, so the host never learns an
    address. This assigns a static IP to the RNDIS adapter and ARP-sweeps the
    likely subnets. ARP is the right tool: it answers at layer 2, so it finds
    the camera whatever its IP config, and silence across every subnet is
    strong evidence the camera has no IP stack running at all.

    Run from an ELEVATED PowerShell:
        powershell -ExecutionPolicy Bypass -File tools\probe_rndis.ps1

    If the camera is in webcam mode (PID_0023) the script waits for you to
    switch it back rather than giving up.

    On success the working static config is left in place so the Python driver
    can use it immediately. On failure the adapter is put back to DHCP.
#>

[CmdletBinding()]
param(
    # $PSScriptRoot is empty when the body is pasted straight into a shell.
    [string]$LogPath = $(if ($PSScriptRoot) { "$PSScriptRoot\..\rndis-probe.log" }
                         else { (Join-Path (Get-Location) "rndis-probe.log") }),
    # Sweep these fully; everything else gets a spot check.
    [string[]]$FullSweep = @("192.168.2", "192.168.42"),
    [string[]]$SpotCheck = @("192.168.0", "192.168.1", "192.168.43",
                             "192.168.100", "10.0.0", "172.16.0"),
    [int]$WaitSeconds = 120,
    [switch]$RestoreAlways
)

$ErrorActionPreference = "Continue"
$script:found = @()
$script:IfName = $null

function Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding utf8
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-DjiPid {
    $dji = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_2CA3' }
    if (-not $dji) { return $null }
    if ($dji[0].InstanceId -match 'PID_([0-9A-Fa-f]{4})') { return $Matches[1] }
    return "????"
}

# Never hardcode the adapter name: a USB mode change re-enumerates the device
# and Windows hands out the next free index ("Ethernet 3", "Ethernet 4", ...).
function Get-RndisAdapter {
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceDescription -match 'Remote NDIS|RNDIS' } |
        Sort-Object -Property @{ Expression = { $_.Status -eq 'Up' } } -Descending |
        Select-Object -First 1
}

function Wait-ForRndis {
    param([int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    $announced = $false
    while ((Get-Date) -lt $deadline) {
        $pid_ = Get-DjiPid
        $adapter = Get-RndisAdapter
        if ($adapter -and $adapter.Status -eq 'Up') {
            Log "RNDIS adapter '$($adapter.Name)' is Up at $($adapter.LinkSpeed)"
            return $adapter
        }
        if (-not $announced) {
            if ($null -eq $pid_) {
                Log "waiting: no DJI device on USB. Plug the camera in."
            } elseif ($pid_ -eq "0023") {
                Log "waiting: camera is in WEBCAM mode (PID_0023), which has no RNDIS."
                Log "         Switch it to the default / file-transfer USB mode."
                Log "         (Unplug and replug, then pick the non-webcam option,"
                Log "          or exit webcam from the camera touchscreen.)"
            } else {
                Log "waiting: PID_$pid_ present but no RNDIS adapter yet."
            }
            Log "waiting up to ${Seconds}s ..."
            $announced = $true
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

function Clear-Peers {
    Get-NetNeighbor -InterfaceAlias $script:IfName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.State -ne "Permanent" } |
        Remove-NetNeighbor -Confirm:$false -ErrorAction SilentlyContinue
}

function Get-Peers {
    Get-NetNeighbor -InterfaceAlias $script:IfName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LinkLayerAddress -and
            $_.LinkLayerAddress -notmatch '^(FF-FF-FF|01-00-5E|00-00-00)' -and
            $_.State -match 'Reachable|Stale|Delay|Probe'
        }
}

function Set-StaticIp {
    param([string]$Address)
    Set-NetIPInterface -InterfaceAlias $script:IfName -Dhcp Disabled -ErrorAction SilentlyContinue
    Get-NetIPAddress -InterfaceAlias $script:IfName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    New-NetIPAddress -InterfaceAlias $script:IfName -IPAddress $Address `
        -PrefixLength 24 -ErrorAction Stop | Out-Null
    Start-Sleep -Milliseconds 500
}

function Restore-Dhcp {
    if (-not $script:IfName) { return }
    Get-NetIPAddress -InterfaceAlias $script:IfName -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $script:IfName -Dhcp Enabled -ErrorAction SilentlyContinue
    Log "adapter restored to DHCP"
}

function Sweep-Subnet {
    param([string]$Net, [int[]]$Hosts)

    $self = "$Net.100"
    try { Set-StaticIp $self }
    catch { Log "  cannot set $self -- $($_.Exception.Message)"; return }

    Clear-Peers
    foreach ($h in $Hosts) {
        if ($h -eq 100) { continue }
        # -w 60 keeps a full /24 near 15s. Source-bound so nothing can leak
        # out of the default route and produce a phantom reply.
        ping -n 1 -w 60 -S $self "$Net.$h" | Out-Null
    }
    Start-Sleep -Milliseconds 400

    $peers = Get-Peers
    if ($peers) {
        foreach ($p in $peers) {
            Log "  FOUND $($p.IPAddress) at $($p.LinkLayerAddress) [$($p.State)]"
            $script:found += [pscustomobject]@{
                Subnet = $Net; Ip = $p.IPAddress
                Mac = $p.LinkLayerAddress; HostIp = $self
            }
        }
    } else {
        Log "  $Net.0/24 silent"
    }
}

function Test-CameraPorts {
    param([string]$Target)
    foreach ($port in 7001, 9004, 80, 8080, 554) {
        $client = New-Object Net.Sockets.TcpClient
        $ok = $client.BeginConnect($Target, $port, $null, $null).AsyncWaitHandle.WaitOne(1200)
        $state = if ($ok -and $client.Connected) { "OPEN" } else { "closed" }
        Log ("  TCP {0,-5} {1}" -f $port, $state)
        $client.Close()
    }
}

# --- run ---------------------------------------------------------------------

"" | Set-Content -Path $LogPath -Encoding utf8
Log "=== Osmo RNDIS probe ==="

if (-not (Test-Elevated)) {
    Log "NOT ELEVATED -- static IP assignment will fail."
    Log "Re-run from an Administrator PowerShell."
    exit 1
}
Log "elevated: yes"

$pid_ = Get-DjiPid
Log $(if ($pid_) { "DJI device present, PID_$pid_" } else { "no DJI device on USB yet" })

$adapter = Get-RndisAdapter
if (-not ($adapter -and $adapter.Status -eq 'Up')) {
    $adapter = Wait-ForRndis -Seconds $WaitSeconds
}
if (-not $adapter) {
    Log ""
    Log "RESULT: no RNDIS adapter appeared within ${WaitSeconds}s."
    Log "Confirm the camera is OUT of webcam mode, then re-run."
    exit 1
}

$script:IfName = $adapter.Name
Log "using adapter '$($script:IfName)' -- $($adapter.InterfaceDescription)"
$pid_ = Get-DjiPid
Log "camera now PID_$pid_"

try {
    foreach ($net in $FullSweep) {
        Log "full sweep $net.0/24 ..."
        Sweep-Subnet -Net $net -Hosts (1..254)
    }
    foreach ($net in $SpotCheck) {
        Log "spot check $net.0/24 ..."
        Sweep-Subnet -Net $net -Hosts @(1, 2, 10, 100, 200, 254)
    }

    Log ""
    if ($script:found.Count -eq 0) {
        Log "RESULT: no peer on any subnet tried."
        Log "The camera's RNDIS function has no IP stack running in this mode."
        Log "Remaining USB option is the unbound BULK interfaces (WinUSB/libusb)."
        Restore-Dhcp
    } else {
        $hit = $script:found[0]
        Log "RESULT: camera at $($hit.Ip) (host $($hit.HostIp)), MAC $($hit.Mac)"
        Log "probing camera ports ..."
        Set-StaticIp $hit.HostIp
        Test-CameraPorts $hit.Ip
        Log ""
        Log "Static config LEFT IN PLACE. Next:"
        Log "  .venv\Scripts\python.exe run.py --skip-ble --skip-wifi-join --host $($hit.Ip) --no-imu --demo"
        if ($RestoreAlways) { Restore-Dhcp }
    }
} catch {
    Log "ERROR: $($_.Exception.Message)"
    Restore-Dhcp
}

Log "log written to $LogPath"
