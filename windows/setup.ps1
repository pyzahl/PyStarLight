<#
PyStarLight setup for Windows: install, update or uninstall, per user (no administrator rights).

Normally run by PyStarLight-Setup.cmd (double-click; it is this script behind a small cmd header,
built by scripts/build_windows_setup.py). Later the Start menu's "Update PyStarLight" and
"Uninstall PyStarLight" run the copy in %LOCALAPPDATA%\PyStarLight\setup.ps1.

What it does:
  1. uv (astral.sh) into %LOCALAPPDATA%\PyStarLight\bin: a private copy, PATH is not changed.
  2. PyStarLight from the GitHub release (wheel + constraints.txt: the tested package versions),
     `uv tool install` with uv's own Python (it has Tk), extra "desktop" (all + Qt + pyserial).
  3. Start menu folder PyStarLight: Analyze, Player, Camera, Settings, Check installation, Logs,
     Update, Uninstall; desktop icons Analyze, Player, Camera.
  4. "Open with": the player and the analyzer for .ravf .ser .adv .aav (double-click opens the player
     unless another program owns the type) and .avi (Open with only).
  5. `pystarlight doctor`: one line per part.

  PyStarLight-Setup.cmd                    install or update to the latest release
  PyStarLight-Setup.cmd -Version v1.1.0    a given release
  PyStarLight-Setup.cmd -Uninstall         remove (keeps the settings: %APPDATA%\pystarlight)
  PyStarLight-Setup.cmd -Uninstall -Purge  remove the settings and logs as well
  -Wheel X.whl -Constraints c.txt          install a local build (tests); -NoDesktop no desktop icons
  -Force                                   end running PyStarLight programs without asking
#>
param(
    [switch]$Uninstall,
    [switch]$Purge,
    [string]$Version = 'latest',
    [string]$Wheel = '',
    [string]$Constraints = '',
    [switch]$NoDesktop,
    [switch]$Force,
    [switch]$Pause
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'        # Invoke-WebRequest crawls with a progress bar (PS 5.1)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = 'pyzahl/PyStarLight'
$Pkg = 'starlight-photometry'
$App = Join-Path $env:LOCALAPPDATA 'PyStarLight'
$Bin = Join-Path $App 'bin'
$Tools = Join-Path $App 'tools'
$PyDir = Join-Path $App 'python'
$Uv = Join-Path $Bin 'uv.exe'
$Icon = Join-Path $App 'pystarlight.ico'
$StartDir = Join-Path ([Environment]::GetFolderPath('Programs')) 'PyStarLight'
$DesktopDir = [Environment]::GetFolderPath('Desktop')
$Recordings = @('.ravf', '.ser', '.adv', '.aav')
$OpenWithOnly = @('.avi')
$ProgIds = @{ 'PyStarLight.Player' = @('pystarlight-player-gui.exe', 'PyStarLight Player');
              'PyStarLight.Analyze' = @('pystarlight-analyze-gui.exe', 'PyStarLight Analyze') }

$env:UV_TOOL_DIR = $Tools
$env:UV_TOOL_BIN_DIR = $Bin
$env:UV_PYTHON_INSTALL_DIR = $PyDir
$env:UV_NO_MODIFY_PATH = '1'
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue

function Say([string]$msg) { Write-Host $msg }
function Step([string]$msg) { Write-Host ''; Write-Host "== $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host ''; Write-Host "ERROR: $msg" -ForegroundColor Red; Finish 1 }
function Finish([int]$code) {
    if ($Pause) { Write-Host ''; Read-Host 'Press Enter to close' | Out-Null }
    exit $code
}

function Invoke-Native([scriptblock]$cmd) {
    # Windows PowerShell 5.1 turns a native program's stderr into errors, which 'Stop' would make
    # fatal: run with 'Continue' and judge by the exit code
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $cmd } finally { $ErrorActionPreference = $old }
}

function Running-Programs {
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($App, [StringComparison]::OrdinalIgnoreCase) })
}

function Ensure-Closed([string]$what) {
    # Windows does not let files of a running program be replaced or deleted
    $p = Running-Programs
    if (-not $p) { return }
    $names = ($p | ForEach-Object { $_.ProcessName } | Sort-Object -Unique) -join ', '
    Write-Host ''
    Write-Host "   PyStarLight programs are running: $names" -ForegroundColor Yellow
    if ($Force) {
        $p | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    } elseif ($Pause) {
        $a = Read-Host '   Close them (a camera recording: stop it first), then press Enter; or type STOP to end them now'
        if ($a -match '^\s*stop\s*$') {
            Running-Programs | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }
    if (Running-Programs) { Fail "close the PyStarLight programs, then $what again (or run it with -Force)" }
}

function Download([string]$url, [string]$out) {
    Say "   $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -Headers @{ 'User-Agent' = 'PyStarLight-setup' }
}

# --------------------------------------------------------------------------------------------- uv
function Get-Uv {
    if (Test-Path $Uv) { return }
    Step 'uv (package manager, private copy)'
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
    $zip = Join-Path $env:TEMP "uv-$arch.zip"
    Download "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip" $zip
    $tmp = Join-Path $env:TEMP 'pystarlight-uv'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    Get-ChildItem $tmp -Recurse -Include 'uv.exe', 'uvx.exe' | ForEach-Object { Copy-Item $_.FullName $Bin -Force }
    Remove-Item $tmp -Recurse -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $Uv)) { Fail 'uv.exe not found in the download' }
    Say ('   ' + (& $Uv --version))
}

# --------------------------------------------------------------------------------------------- release
function Get-Release {
    if ($Wheel) {
        if (-not (Test-Path $Wheel)) { Fail "no such wheel: $Wheel" }
        return @{ tag = 'local'; wheel = (Resolve-Path $Wheel).Path;
                  constraints = $(if ($Constraints) { (Resolve-Path $Constraints).Path } else { '' }) }
    }
    Step "PyStarLight release ($Version)"
    $hdr = @{ 'User-Agent' = 'PyStarLight-setup' }
    $rel = $null
    if ($Version -eq 'latest') {
        # the newest release that carries the Windows files (a release made before them has none)
        try {
            $all = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" -UseBasicParsing -Headers $hdr
            $rel = $all | Where-Object { -not $_.draft -and -not $_.prerelease -and
                                         ($_.assets | Where-Object { $_.name -like '*.whl' }) } |
                   Select-Object -First 1
        } catch {
            Say "   cannot read the releases from GitHub: $($_.Exception.Message)"
        }
        if (-not $rel) {
            # no release with a wheel yet: the current source from GitHub (built here, a minute more)
            Say '   no release with the Windows files yet: installing the current version from GitHub'
            return @{ tag = 'main'; constraints = '';
                      wheel = "https://github.com/$Repo/archive/refs/heads/main.zip" }
        }
    } else {
        $api = "https://api.github.com/repos/$Repo/releases/tags/$Version"
        try {
            $rel = Invoke-RestMethod -Uri $api -UseBasicParsing -Headers $hdr
        } catch {
            Fail "no release $Version on GitHub ($($_.Exception.Message))"
        }
    }
    $w = $rel.assets | Where-Object { $_.name -like '*.whl' } | Select-Object -First 1
    $c = $rel.assets | Where-Object { $_.name -like 'constraints*.txt' } | Select-Object -First 1
    if (-not $w) { Fail "release $($rel.tag_name) has no Windows files (made before the Windows setup)" }
    $dl = Join-Path $App 'downloads'
    if (Test-Path $dl) { Remove-Item $dl -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dl | Out-Null
    $wp = Join-Path $dl $w.name
    Download $w.browser_download_url $wp
    $cp = ''
    if ($c) { $cp = Join-Path $dl $c.name; Download $c.browser_download_url $cp }
    Say "   release $($rel.tag_name)"
    return @{ tag = $rel.tag_name; wheel = $wp; constraints = $cp }
}

function Installed-Version {
    $exe = Join-Path $Bin 'pystarlight.exe'
    if (-not (Test-Path $exe)) { return '' }
    try { return ((& $exe --version) -replace 'PyStarLight ', '').Trim() } catch { return '' }
}

function Install-Package($rel) {
    Step 'PyStarLight (Python with Tk, packages; the first time takes a few minutes)'
    $uri = ([Uri]$rel.wheel).AbsoluteUri
    $uvArgs = @('tool', 'install', '--force', '--python-preference', 'only-managed', '--python', '3.12',
                "$($Pkg)[desktop] @ $uri")
    if ($rel.constraints) { $uvArgs += @('--constraints', $rel.constraints) }
    Invoke-Native { & $Uv @uvArgs }
    if ($LASTEXITCODE -ne 0) { Fail "uv tool install failed (exit $LASTEXITCODE)" }
    Say ('   ' + (& (Join-Path $Bin 'pystarlight.exe') --version))
}

# --------------------------------------------------------------------------------------------- desktop
function New-Shortcut([string]$path, [string]$target, [string]$arguments, [string]$description) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.Description = $description
    $lnk.WorkingDirectory = [Environment]::GetFolderPath('MyDocuments')
    if (Test-Path $Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
}

function Install-Shortcuts {
    Step 'Start menu and desktop'
    $py = Join-Path $Tools "$Pkg\Scripts\python.exe"
    & $py -m pystarlight.winlaunch icon $Icon | Out-Null
    if (Test-Path $StartDir) { Remove-Item $StartDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $StartDir | Out-Null
    Copy-Item $PSCommandPath (Join-Path $App 'setup.ps1') -Force -ErrorAction SilentlyContinue
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $setup = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $App 'setup.ps1')`""
    $items = @(
        @('PyStarLight Analyze', (Join-Path $Bin 'pystarlight-analyze-gui.exe'), '', 'Analyse a recording: photometry, D / R times'),
        @('PyStarLight Player', (Join-Path $Bin 'pystarlight-player-gui.exe'), '', 'Play a recording with its time stamps'),
        @('PyStarLight Camera', (Join-Path $Bin 'pystarlight-camera-gui.exe'), '', 'Record (capture program)'),
        @('Settings', (Join-Path $Bin 'pystarlight-settings-gui.exe'), '', 'PyStarLight settings (config.toml)'),
        @('Check installation', (Join-Path $env:SystemRoot 'System32\cmd.exe'), "/k `"`"$(Join-Path $Bin 'pystarlight.exe')`" doctor --extras desktop`"", 'pystarlight doctor'),
        @('Logs', (Join-Path $env:SystemRoot 'explorer.exe'), "`"$(Join-Path $App 'logs')`"", 'The programs'' log files'),
        @('Update PyStarLight', $ps, "$setup -Pause", 'Install the latest release'),
        @('Uninstall PyStarLight', $ps, "$setup -Uninstall -Pause", 'Remove PyStarLight (keeps the settings)')
    )
    New-Item -ItemType Directory -Force -Path (Join-Path $App 'logs') | Out-Null
    foreach ($i in $items) { New-Shortcut (Join-Path $StartDir "$($i[0]).lnk") $i[1] $i[2] $i[3] }
    if (-not $NoDesktop) {
        foreach ($i in $items[0..2]) { New-Shortcut (Join-Path $DesktopDir "$($i[0]).lnk") $i[1] $i[2] $i[3] }
    }
    Say "   $StartDir"
}

function Install-FileTypes {
    Step 'Open with: recordings'
    $cls = 'HKCU:\Software\Classes'
    foreach ($id in $ProgIds.Keys) {
        $exe = Join-Path $Bin $ProgIds[$id][0]
        New-Item -Path "$cls\$id\shell\open\command" -Force | Out-Null
        Set-Item -Path "$cls\$id" -Value "Recording ($($ProgIds[$id][1]))"
        Set-ItemProperty -Path "$cls\$id" -Name 'FriendlyTypeName' -Value 'Occultation recording'
        New-Item -Path "$cls\$id\DefaultIcon" -Force | Out-Null
        Set-Item -Path "$cls\$id\DefaultIcon" -Value "$Icon,0"
        Set-Item -Path "$cls\$id\shell\open\command" -Value "`"$exe`" `"%1`""
        $app = "$cls\Applications\$($ProgIds[$id][0])"
        New-Item -Path "$app\shell\open\command" -Force | Out-Null
        Set-ItemProperty -Path $app -Name 'FriendlyAppName' -Value $ProgIds[$id][1]
        Set-Item -Path "$app\shell\open\command" -Value "`"$exe`" `"%1`""
        New-Item -Path "$app\SupportedTypes" -Force | Out-Null
        foreach ($ext in $Recordings + $OpenWithOnly) {
            Set-ItemProperty -Path "$app\SupportedTypes" -Name $ext -Value ''
        }
    }
    foreach ($ext in $Recordings + $OpenWithOnly) {
        New-Item -Path "$cls\$ext\OpenWithProgids" -Force | Out-Null
        foreach ($id in $ProgIds.Keys) { Set-ItemProperty -Path "$cls\$ext\OpenWithProgids" -Name $id -Value '' }
    }
    foreach ($ext in $Recordings) {        # double-click: the player, when nothing else owns the type
        $cur = (Get-ItemProperty -Path "$cls\$ext" -ErrorAction SilentlyContinue).'(default)'
        $hklm = (Get-ItemProperty -Path "HKLM:\Software\Classes\$ext" -ErrorAction SilentlyContinue).'(default)'
        if (-not $cur -and -not $hklm) { Set-Item -Path "$cls\$ext" -Value 'PyStarLight.Player' }
    }
    Notify-Shell
    Say "   $($Recordings -join ' ') (player; Open with also the analyzer), $($OpenWithOnly -join ' ') (Open with)"
}

function Notify-Shell {
    try {
        Add-Type -Namespace PSL -Name Shell -MemberDefinition @'
[DllImport("shell32.dll")] public static extern void SHChangeNotify(int e, int f, System.IntPtr a, System.IntPtr b);
'@ -ErrorAction SilentlyContinue
        [PSL.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } catch { }
}

# --------------------------------------------------------------------------------------------- uninstall
function Remove-All {
    Step 'Uninstall PyStarLight'
    Ensure-Closed 'uninstall'
    $cls = 'HKCU:\Software\Classes'
    foreach ($ext in $Recordings + $OpenWithOnly) {
        foreach ($id in $ProgIds.Keys) {
            Remove-ItemProperty -Path "$cls\$ext\OpenWithProgids" -Name $id -ErrorAction SilentlyContinue
        }
        $cur = (Get-ItemProperty -Path "$cls\$ext" -ErrorAction SilentlyContinue).'(default)'
        if ($cur -like 'PyStarLight.*') { Set-Item -Path "$cls\$ext" -Value '' }
    }
    foreach ($id in $ProgIds.Keys) {
        Remove-Item -Path "$cls\$id" -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -Path "$cls\Applications\$($ProgIds[$id][0])" -Recurse -Force -ErrorAction SilentlyContinue
    }
    Notify-Shell
    if (Test-Path $StartDir) { Remove-Item $StartDir -Recurse -Force }
    foreach ($n in 'PyStarLight Analyze', 'PyStarLight Player', 'PyStarLight Camera') {
        Remove-Item (Join-Path $DesktopDir "$n.lnk") -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $Uv) { Invoke-Native { & $Uv tool uninstall $Pkg 2>&1 | Out-Null } }
    foreach ($d in 'tools', 'python', 'downloads', 'bin') {
        $p = Join-Path $App $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item $Icon, (Join-Path $App 'setup.ps1') -Force -ErrorAction SilentlyContinue
    $left = @('tools', 'python', 'bin' | Where-Object { Test-Path (Join-Path $App $_) })
    if ($left) {
        Write-Host "   could not remove $($left -join ', ') in $App (a file in use? sign out and in, then" -ForegroundColor Yellow
        Write-Host '   delete the folder by hand)' -ForegroundColor Yellow
    }
    if ($Purge) {
        Remove-Item $App -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $env:APPDATA 'pystarlight') -Recurse -Force -ErrorAction SilentlyContinue
        Say '   settings and logs removed too'
    } else {
        Say "   settings kept: $(Join-Path $env:APPDATA 'pystarlight'); logs: $(Join-Path $App 'logs')"
    }
    Say '   done'
}

# --------------------------------------------------------------------------------------------- main
Write-Host 'PyStarLight setup' -ForegroundColor Green
if ($Uninstall) {
    # running from the installed copy: continue from a temporary copy, so the folder can go
    if ($PSCommandPath -and $PSCommandPath.StartsWith($App, [StringComparison]::OrdinalIgnoreCase)) {
        $tmp = Join-Path $env:TEMP 'PyStarLight-uninstall.ps1'
        Copy-Item $PSCommandPath $tmp -Force
        $rest = @('-Uninstall')
        if ($Purge) { $rest += '-Purge' }
        if ($Pause) { $rest += '-Pause' }
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -ExecutionPolicy Bypass -File $tmp @rest
        exit $LASTEXITCODE
    }
    Remove-All
    Finish 0
}

New-Item -ItemType Directory -Force -Path $App | Out-Null
Get-Uv
$before = Installed-Version
$rel = Get-Release
if ($before -and $rel.tag -ne 'local' -and ('v' + $before) -eq $rel.tag) {
    Step "PyStarLight $before is the latest release: nothing to install"
} else {
    Ensure-Closed 'the setup'           # also for a damaged install whose version cannot be read
    Install-Package $rel
}
Install-Shortcuts
Install-FileTypes

Step 'Check (pystarlight doctor)'
Invoke-Native { & (Join-Path $Bin 'pystarlight.exe') doctor --extras desktop }
$doctor = $LASTEXITCODE

Write-Host ''
Write-Host "PyStarLight $(Installed-Version) is installed." -ForegroundColor Green
Say '  Start menu: PyStarLight -> Analyze, Player, Camera, Settings, Check installation, Update, Uninstall'
Say '  Plate solving offline: install ASTAP and its D80 star database from www.hnsky.org;'
Say '  PyStarLight finds it. Else online at nova.astrometry.net with your free API key'
Say '  (Start menu -> Settings, [astrometry] api_key).'
if ($doctor -ne 0) { Say '  The check above found something missing: see its "fix:" lines.' }
Finish 0
