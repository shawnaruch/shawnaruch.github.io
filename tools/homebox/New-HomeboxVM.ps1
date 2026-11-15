#Requires -RunAsAdministrator
#Requires -Modules Hyper-V
<#
.SYNOPSIS
  Creates the "homebox" Ubuntu Server VM on this Hyper-V host. It runs the
  network scanner, the SensorPush collector and the local dashboards.

.DESCRIPTION
  - Bridges the VM onto your real LAN with an External switch. Hyper-V's
    "Default Switch" is NAT, so the VM couldn't see mDNS/SSDP or your devices.
  - Gives the VM a fixed MAC so you can reserve its IP in the eero app.
  - Starts the VM automatically with the host. Checkpoints are off.
  - Downloads Ubuntu Server 26.04.1 LTS and checks its SHA-256 first.

  Run in an elevated PowerShell on the Hyper-V host:
    Set-ExecutionPolicy -Scope Process Bypass
    .\New-HomeboxVM.ps1
    .\New-HomeboxVM.ps1 -NetAdapterName "Ethernet"   # if it can't pick one
#>
param(
  [string]$VMName = "homebox",
  [string]$SwitchName = "LAN",
  [string]$NetAdapterName,
  [string]$VMRoot = "C:\Hyper-V",
  [int]$CPUs = 2,
  [int64]$MemoryGB = 4,
  [int64]$DiskGB = 64,
  [string]$IsoName = "ubuntu-26.04.1-live-server-amd64.iso",
  [string]$IsoBase = "https://releases.ubuntu.com/26.04"
)
$ErrorActionPreference = "Stop"

if (Get-VM -Name $VMName -ErrorAction SilentlyContinue) {
  throw "A VM named '$VMName' already exists. Pick another -VMName or remove it first."
}

# --- Ubuntu ISO, verified (before the switch, which briefly drops the network) -----------------------------------------------------
$isoDir = Join-Path $VMRoot "ISO"
New-Item -ItemType Directory -Force -Path $isoDir | Out-Null
$iso = Join-Path $isoDir $IsoName
if (-not (Test-Path $iso)) {
  Write-Host "Downloading $IsoName (about 3 GB)..."
  $ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest ~10x faster
  Invoke-WebRequest "$IsoBase/$IsoName" -OutFile $iso
}
$sums = (Invoke-WebRequest "$IsoBase/SHA256SUMS" -UseBasicParsing).Content
$expected = ($sums -split "`n" | Where-Object { $_ -match [regex]::Escape($IsoName) } | ForEach-Object { ($_ -split "\s+")[0] })
$actual = (Get-FileHash $iso -Algorithm SHA256).Hash
if (-not $expected -or $actual -ne $expected.ToUpper()) {
  Remove-Item $iso
  throw "ISO checksum mismatch. Deleted the download. Re-run to fetch it again."
}
Write-Host "ISO verified."

# --- External switch bound to a wired adapter -------------------------------
$switch = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
if (-not $switch) {
  if (-not $NetAdapterName) {
    $up = Get-NetAdapter -Physical | Where-Object Status -eq "Up"
    $wired = $up | Where-Object { $_.NdisPhysicalMedium -eq 14 }   # 14 = 802.3 Ethernet
    $pick = if ($wired) { $wired } else { $up }
    if (@($pick).Count -ne 1) {
      $up | Format-Table Name, InterfaceDescription, Status, LinkSpeed
      throw "Can't choose an adapter automatically. Re-run with -NetAdapterName <Name> from the table above."
    }
    $NetAdapterName = @($pick)[0].Name
    if (-not $wired) { Write-Warning "Using Wi-Fi adapter '$NetAdapterName'. Bridging over Wi-Fi often breaks mDNS. Wired is strongly preferred." }
  }
  Write-Host "Creating External switch '$SwitchName' on '$NetAdapterName'. The host's network drops for a few seconds."
  New-VMSwitch -Name $SwitchName -NetAdapterName $NetAdapterName -AllowManagementOS $true | Out-Null
}

# --- VM -----------------------------------------------------------------------
$vhd = Join-Path $VMRoot "$VMName\$VMName.vhdx"
New-VM -Name $VMName -Generation 2 -MemoryStartupBytes ($MemoryGB * 1GB) `
  -NewVHDPath $vhd -NewVHDSizeBytes ($DiskGB * 1GB) -SwitchName $SwitchName -Path $VMRoot | Out-Null

Set-VMProcessor -VMName $VMName -Count $CPUs
Set-VMMemory -VMName $VMName -DynamicMemoryEnabled $false   # steady RAM for a 24/7 server
Set-VM -Name $VMName -AutomaticStartAction Start -AutomaticStartDelay 30 `
  -AutomaticStopAction ShutDown -CheckpointType Disabled
Set-VMFirmware -VMName $VMName -SecureBootTemplate MicrosoftUEFICertificateAuthority   # required for Linux
Enable-VMIntegrationService -VMName $VMName -Name "Guest Service Interface"

# Pin a static MAC so the eero reservation survives VM rebuilds.
$adapter = Get-VMNetworkAdapter -VMName $VMName
Start-VM $VMName; Start-Sleep 3; Stop-VM $VMName -TurnOff -Force   # let Hyper-V assign one
$mac = (Get-VMNetworkAdapter -VMName $VMName).MacAddress
Set-VMNetworkAdapter -VMName $VMName -StaticMacAddress $mac

$dvd = Add-VMDvdDrive -VMName $VMName -Path $iso -Passthru
Set-VMFirmware -VMName $VMName -FirstBootDevice $dvd

$pretty = ($mac -split "(..)" | Where-Object { $_ }) -join ":"
Write-Host ""
Write-Host "Created '$VMName'. MAC address: $pretty"
Write-Host "Next:"
Write-Host "  1. eero app > Settings > Network settings > Reservations & port forwarding: reserve an IP for $pretty"
Write-Host "  2. Start-VM $VMName ; vmconnect localhost $VMName"
Write-Host "  3. In the installer: tick 'Install OpenSSH server', skip the extra snaps."
Write-Host "  4. After install: Remove-VMDvdDrive -VMName $VMName -ControllerNumber $($dvd.ControllerNumber) -ControllerLocation $($dvd.ControllerLocation)"
