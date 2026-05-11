$ErrorActionPreference = "SilentlyContinue"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    $python = $venvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
} else {
    Write-Error "No Python interpreter found."
    exit 1
}

$scriptPath = Join-Path $repoRoot "hooks\log_event.py"
if ($python -eq "py") {
    & py -3 $scriptPath --client codex
} else {
    & $python $scriptPath --client codex
}
