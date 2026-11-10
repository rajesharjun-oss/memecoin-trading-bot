# Registers the existing local paper dashboard at sign-in without storing keys
# or a pairing code in Task Scheduler. Run from a normal, non-admin PowerShell.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'MemeOps Paper Dashboard (Free Only)'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$envPath = Join-Path $projectRoot '.env'
$arguments = '-u -m memecoin_bot.cli dashboard --no-browser --port 8787 --free-only'
$previousArguments = '-u -m memecoin_bot.cli dashboard --no-browser --port 8787'
$userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$settings = New-ScheduledTaskSettingsSet `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

function Ensure-PaperWatchdogTrigger {
    param([string]$Name)

    $definition = [xml](Export-ScheduledTask -TaskName $Name -ErrorAction Stop)
    $ns = [System.Xml.XmlNamespaceManager]::new($definition.NameTable)
    $taskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    $ns.AddNamespace('t', $taskNamespace)
    $triggersNode = $definition.SelectSingleNode('/t:Task/t:Triggers', $ns)
    $triggers = $triggersNode.SelectNodes('*', $ns)
    $logon = $definition.SelectSingleNode('/t:Task/t:Triggers/t:LogonTrigger', $ns)
    $timeTrigger = $definition.SelectSingleNode('/t:Task/t:Triggers/t:TimeTrigger', $ns)
    $expectedCount = if ($null -eq $timeTrigger) { 1 } else { 2 }
    if ($null -eq $logon -or $triggers.Count -ne $expectedCount) {
        throw 'The existing task has unexpected triggers; review it manually.'
    }
    $logonRepetition = $logon.SelectSingleNode('t:Repetition', $ns)
    $repetition = if ($null -ne $timeTrigger) {
        $timeTrigger.SelectSingleNode('t:Repetition', $ns)
    } else { $null }
    $interval = if ($null -ne $repetition) { $repetition.SelectSingleNode('t:Interval', $ns) } else { $null }
    $duration = if ($null -ne $repetition) { $repetition.SelectSingleNode('t:Duration', $ns) } else { $null }
    if ($null -eq $logonRepetition -and $null -ne $timeTrigger -and
        $null -ne $interval -and $interval.InnerText -eq 'PT5M' -and $null -eq $duration) {
        return
    }
    if ($null -ne $logonRepetition) { [void]$logon.RemoveChild($logonRepetition) }
    if ($null -eq $timeTrigger) {
        $timeTrigger = $definition.CreateElement('TimeTrigger', $taskNamespace)
        $start = $definition.CreateElement('StartBoundary', $taskNamespace)
        $start.InnerText = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
        [void]$timeTrigger.AppendChild($start)
        [void]$triggersNode.AppendChild($timeTrigger)
    }
    if ($null -eq $repetition) {
        $repetition = $definition.CreateElement('Repetition', $taskNamespace)
        [void]$timeTrigger.PrependChild($repetition)
    }
    if ($null -eq $interval) {
        $interval = $definition.CreateElement('Interval', $taskNamespace)
        [void]$repetition.PrependChild($interval)
    }
    $interval.InnerText = 'PT5M'
    if ($null -ne $duration) { [void]$repetition.RemoveChild($duration) }

    Register-ScheduledTask -TaskName $Name -Xml $definition.OuterXml -Force -ErrorAction Stop | Out-Null
    $verified = [xml](Export-ScheduledTask -TaskName $Name -ErrorAction Stop)
    $verifiedInterval = $verified.SelectSingleNode(
        '/t:Task/t:Triggers/t:TimeTrigger/t:Repetition/t:Interval', $ns
    )
    $verifiedDuration = $verified.SelectSingleNode(
        '/t:Task/t:Triggers/t:TimeTrigger/t:Repetition/t:Duration', $ns
    )
    $verifiedLogonRepeat = $verified.SelectSingleNode(
        '/t:Task/t:Triggers/t:LogonTrigger/t:Repetition', $ns
    )
    if ($null -eq $verifiedInterval -or $verifiedInterval.InnerText -ne 'PT5M' -or
        $null -ne $verifiedDuration -or $null -ne $verifiedLogonRepeat) {
        throw 'The five-minute paper watchdog trigger was not saved.'
    }
    Write-Host 'Added a five-minute relaunch check while this user is signed in.'
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project virtual environment is missing. Install the bot before registering the task.'
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw 'Project .env is missing. Configure the paper bot before registering the task.'
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -c 'from memecoin_bot.config import Settings; from memecoin_bot.constants import LIVE_EXECUTION_LOCKED; s=Settings.from_env(); assert LIVE_EXECUTION_LOCKED and not s.live_trading_enabled and not s.narrative_openai_enabled, "Paper/free-only safety checks failed"'
    if ($LASTEXITCODE -ne 0) {
        throw 'Paper/free-only safety checks failed. Keep live execution and paid OpenAI research disabled.'
    }
}
finally {
    Pop-Location
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    # The CIM view may shorten DOMAIN\user to user. The exported task XML
    # retains the exact account SID, which is the identity Windows will run.
    $taskXml = [xml](Export-ScheduledTask -TaskName $taskName)
    $namespace = [System.Xml.XmlNamespaceManager]::new($taskXml.NameTable)
    $namespace.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $principalNode = $taskXml.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $namespace)
    $triggerNode = $taskXml.SelectSingleNode('/t:Task/t:Triggers/t:LogonTrigger/t:UserId', $namespace)
    $sameExecutable = $existing.Actions.Count -eq 1 -and
        $existing.Actions[0].Execute -eq $pythonPath -and
        $existing.Actions[0].WorkingDirectory -eq $projectRoot
    if (-not $sameExecutable -or $null -eq $principalNode -or
        $principalNode.InnerText -ne $userSid -or
        $null -eq $triggerNode -or $triggerNode.InnerText -ne $userName -or
        $existing.Principal.LogonType -ne 'Interactive' -or
        $existing.Principal.RunLevel -ne 'Limited' -or
        $existing.Settings.MultipleInstances -ne 'IgnoreNew' -or
        $existing.Settings.RestartCount -ne 999) {
        throw 'A task with this name already exists but has different settings; review it manually.'
    }
    if ($existing.Actions[0].Arguments -eq $previousArguments) {
        $newAction = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectRoot
        Set-ScheduledTask -TaskName $taskName -Action $newAction -ErrorAction Stop | Out-Null
        Write-Host 'Updated the existing task to enforce free-only settings at every sign-in.'
    }
    elseif ($existing.Actions[0].Arguments -ne $arguments) {
        throw 'The existing task uses unexpected arguments; review it manually.'
    }
    if ($existing.Settings.DisallowStartIfOnBatteries -or $existing.Settings.StopIfGoingOnBatteries) {
        Set-ScheduledTask -TaskName $taskName -Settings $settings -ErrorAction Stop | Out-Null
        Write-Host 'Updated the paper dashboard task to start and continue on battery power.'
    }
    Ensure-PaperWatchdogTrigger -Name $taskName
    Write-Host 'The free-only paper dashboard task is already registered for this user.'
    return
}

$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userName
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Local, free-only Solana paper dashboard; live execution remains code-locked.' `
    -ErrorAction Stop | Out-Null

Ensure-PaperWatchdogTrigger -Name $taskName

Write-Host 'Registered for the current Windows user at sign-in. The task was not started now.'
Write-Host 'After sign-in, a valid remembered browser session works without a new code. Otherwise read the code in the dashboard console.'
