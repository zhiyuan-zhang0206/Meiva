param(
  [string]$Root = "D:\meiva-win-bridge",
  [string]$User = "$env:COMPUTERNAME\$env:USERNAME",
  [int]$Port = 18792,
  [string]$TaskName = "MeivaWinBridge"
)

$ErrorActionPreference = "Stop"
$py = "$Root\.venv\Scripts\python.exe"
$pyw = "$Root\.venv\Scripts\pythonw.exe"

Write-Output "=== 1. VERIFY X64 INPUT STRUCT SIZE & IMPORTS ==="
& $py -c "import ctypes, sys; sys.path.insert(0, r'$Root'); from win_helper.input_logic import INPUT; sz = ctypes.sizeof(INPUT); assert sz == 40, f'bad INPUT size {sz}'; print('STRUCT_OK sizeof(INPUT)=' + str(sz))"

Write-Output "=== 2. STOP EXISTING HELPER IF RUNNING ==="
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*win_helper*server*" } | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 400

Write-Output "=== 3. REGISTER INTERACTIVE SESSION 1 SCHEDULED TASK ==="
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m win_helper.server" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Meiva Windows Session 1 UIA Streamable-HTTP MCP Helper (loopback-only)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2
    if ($resp.StatusCode -eq 200) {
      Write-Output ("HEALTHZ=" + $resp.Content)
      $ready = $true
      break
    }
  } catch {}
}

if (-not $ready) {
  Write-Output "FAILED_TO_START"
  if (Test-Path "$Root\logs\stderr.log") { Get-Content "$Root\logs\stderr.log" -Tail 40 }
  if (Test-Path "$Root\logs\bridge.log") { Get-Content "$Root\logs\bridge.log" -Tail 40 }
  exit 1
}
