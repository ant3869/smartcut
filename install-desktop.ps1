# Build SmartCut.exe and put a SmartCut shortcut on the desktop.
# Run after setup.ps1. -StartMenu also adds a Start menu entry.
param([switch]$StartMenu)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Icon = Join-Path $Root 'packaging\smartcut.ico'
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $Pythonw)) { throw 'Run setup.ps1 first: .venv\Scripts\pythonw.exe is missing' }

# The .NET Framework C# compiler ships with Windows, so the exe needs no SDK.
$Csc = @('Framework64', 'Framework') |
    ForEach-Object { Join-Path $env:WINDIR "Microsoft.NET\$_\v4.0.30319\csc.exe" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Csc) {
    $Exe = Join-Path $Root 'SmartCut.exe'
    $Version = ((Get-Content (Join-Path $Root 'VERSION') -Raw).Trim() -replace '[^0-9.].*$', '') + '.0'
    $Info = Join-Path ([IO.Path]::GetTempPath()) 'SmartCut.AssemblyInfo.cs'
    @(
        '[assembly: System.Reflection.AssemblyTitle("SmartCut")]'
        '[assembly: System.Reflection.AssemblyProduct("SmartCut")]'
        '[assembly: System.Reflection.AssemblyDescription("Local AI video editing for creators")]'
        "[assembly: System.Reflection.AssemblyVersion(`"$Version`")]"
        "[assembly: System.Reflection.AssemblyFileVersion(`"$Version`")]"
    ) | Set-Content $Info
    & $Csc /nologo /target:winexe /optimize+ "/win32icon:$Icon" "/out:$Exe" `
        /reference:System.Windows.Forms.dll (Join-Path $Root 'packaging\SmartCut.cs') $Info
    $Built = $LASTEXITCODE -eq 0
    Remove-Item $Info
    if (-not $Built) { throw 'SmartCut.exe build failed' }
    Write-Host "built: $Exe"
    $Target, $Arguments = $Exe, ''
} else {
    Write-Warning 'No C# compiler found; the shortcut starts the Python launcher directly.'
    $Target, $Arguments = $Pythonw, '-m pipeline.launcher'
}

$Folders = @([Environment]::GetFolderPath('Desktop'))
if ($StartMenu) { $Folders += [Environment]::GetFolderPath('Programs') }
$Shell = New-Object -ComObject WScript.Shell
foreach ($Folder in $Folders) {
    $Path = Join-Path $Folder 'SmartCut.lnk'
    $Link = $Shell.CreateShortcut($Path)
    $Link.TargetPath = $Target
    $Link.Arguments = $Arguments
    $Link.WorkingDirectory = $Root
    $Link.IconLocation = "$Icon,0"
    $Link.Description = 'SmartCut: local AI video editing'
    $Link.Save()
    Write-Host "shortcut: $Path"
}
