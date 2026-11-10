#!/usr/bin/env pwsh
<#
.SYNOPSIS
Register the neo4j-memory Copilot plugin in VS Code user settings.
#>

$settingsPath = Join-Path $env:APPDATA "Code\User\settings.json"

if (!(Test-Path $settingsPath)) {
    Write-Error "VS Code settings.json not found at $settingsPath"
    Write-Host "Make sure VS Code is installed and has been launched at least once."
    exit 1
}

$pluginPath = (Resolve-Path ".\.copilot").Path

function ConvertTo-Hashtable {
    param([Parameter(ValueFromPipeline = $true)] $InputObject)

    if ($null -eq $InputObject) {
        return $null
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        $ht = @{}
        foreach ($k in $InputObject.Keys) {
            $ht[$k] = ConvertTo-Hashtable $InputObject[$k]
        }
        return $ht
    }

    if ($InputObject -is [pscustomobject]) {
        $ht = @{}
        foreach ($p in $InputObject.PSObject.Properties) {
            $ht[$p.Name] = ConvertTo-Hashtable $p.Value
        }
        return $ht
    }

    if ($InputObject -is [System.Collections.IEnumerable] -and -not ($InputObject -is [string])) {
        $items = @()
        foreach ($item in $InputObject) {
            $items += ,(ConvertTo-Hashtable $item)
        }
        return $items
    }

    return $InputObject
}

$settingsObj = Get-Content $settingsPath -Raw | ConvertFrom-Json
$settings = ConvertTo-Hashtable $settingsObj

if ($null -eq $settings) {
    $settings = @{}
}

if ($null -eq $settings."chat.pluginLocations") {
    $settings["chat.pluginLocations"] = @{}
}

$settings["chat.pluginLocations"][$pluginPath] = $true

$settings | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8

Write-Host "[OK] Plugin registered at: $pluginPath"
Write-Host "[OK] Settings updated: $settingsPath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Reload VS Code (Ctrl+Shift+P -> 'Developer: Reload Window')"
Write-Host "2. Open Copilot Chat"
Write-Host "3. Click the gear icon -> Plugins to verify 'neo4j-memory-hooks' appears"
Write-Host ""
Write-Host "The plugin will now:"
Write-Host "  - Log all chat session events to Neo4j at bolt://localhost:7687"
Write-Host "  - Inject relevant memories from prior sessions into your prompts"
Write-Host "  - Credentials: neo4j / password"
