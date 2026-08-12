param(
    [string]$TaskName = "NXT Dashboard - KIS Universe Reconciliation"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$syncScript = Join-Path $projectRoot "scripts\sync_kis_nxt_universe.py"
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$syncScript`" --reconcile-official" `
    -WorkingDirectory $projectRoot
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "08:10"),
    (New-ScheduledTaskTrigger -Daily -At "18:10")
)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Build the KIS-derived NXT universe and reconcile it with the NXT website locally at 08:10 and 18:10" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
[PSCustomObject]@{
    TaskName = $task.TaskName
    State = $task.State
    NextRunTime = $taskInfo.NextRunTime
    Python = $pythonPath
    Script = $syncScript
}
