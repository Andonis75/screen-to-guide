# Public Windows entry point; all work runs in this Skill's own environment.
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $skillRoot '.runtime\venv\Scripts\python.exe'
$pythonExecutable = $null
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $pythonExecutable = $venvPython
} elseif ($args -contains '--setup') {
    $previousEnvironment = Join-Path (Split-Path -Parent $skillRoot) 'local-doc-guardian\.runtime\venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $previousEnvironment -PathType Leaf) {
        $pythonExecutable = $previousEnvironment
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) { $pythonExecutable = $pythonCommand.Source }
    }
}
if (-not $pythonExecutable) {
    [Console]::Error.WriteLine('Install Python 3.11 on PATH, then run scripts/run.ps1 --setup. uv is optional.')
    exit 1
}
& $pythonExecutable (Join-Path $PSScriptRoot 'run.py') @args
exit $LASTEXITCODE
