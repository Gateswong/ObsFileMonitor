# 启动 OBS 录制叠加工具。
# 首次运行时自动创建虚拟环境（.venv）并安装依赖；requirements.txt 变化后会自动重新安装。
#
# 用法：
#   双击 start.bat（推荐），或在 PowerShell 中运行 .\start.ps1
#   传给程序的参数原样转发，例如：.\start.ps1 --open-config --debug
#
# 注意：本文件必须保存为“UTF-8 带 BOM”，否则 Windows PowerShell 5.1 会把中文读成乱码。

$ErrorActionPreference = 'Stop'
# 文件读写、哈希尽量用 .NET 方法：从 PowerShell 7 中启动 Windows PowerShell 5.1 时会继承 7 的
# 模块路径（PSModulePath），导致部分 cmdlet 无法自动加载（start.bat 会清除该变量，这里再多一层保险）。
Set-Location -LiteralPath $PSScriptRoot

$venvDir = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'requirements.txt'
$requirementsStamp = Join-Path $venvDir 'requirements.sha256'

function Stop-WithError([string]$message) {
    Write-Host ''
    Write-Host $message -ForegroundColor Red
    # 双击运行时窗口会立即关闭，留住窗口让用户看到错误
    if ([Environment]::UserInteractive -and -not $env:OBS_OVERLAY_NO_PAUSE) {
        Read-Host '按回车键退出' | Out-Null
    }
    exit 1
}

function Find-SystemPython {
    # 依次尝试 python.exe 和 Python 启动器 py.exe。
    # Microsoft Store 的“应用执行别名”python.exe 只会打开商店，运行检查时会失败，因此需要真正执行一次。
    foreach ($candidate in @(@('python'), @('py', '-3'))) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $prefix = @(@($candidate)[1..($candidate.Count)] | Where-Object { $_ })
        $ok = & $exe @prefix -c 'import sys, tkinter; print(sys.version_info >= (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $ok -eq 'True') { return , $candidate }
    }
    return $null
}

# ---------- 1. 虚拟环境 ----------
if (-not [IO.File]::Exists($venvPython)) {
    $python = Find-SystemPython
    if (-not $python) {
        Stop-WithError ('未找到可用的 Python。请安装 Python 3.10 或更高版本（https://www.python.org/downloads/），' +
                        '安装时勾选 "Add python.exe to PATH" 和 "tcl/tk and IDLE"。')
    }
    Write-Host "正在创建虚拟环境（$($python -join ' ')）..."
    $exe = $python[0]
    $prefix = @(@($python)[1..($python.Count)] | Where-Object { $_ })
    & $exe @prefix -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { Stop-WithError '创建虚拟环境失败。' }
}

# ---------- 2. 依赖 ----------
$sha = [Security.Cryptography.SHA256]::Create()
try { $hash = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($requirements))) -replace '-', '' }
finally { $sha.Dispose() }
$installed = if ([IO.File]::Exists($requirementsStamp)) { [IO.File]::ReadAllText($requirementsStamp).Trim() } else { '' }
if ($hash -ne $installed) {
    Write-Host '正在安装依赖...'
    & $venvPython -m pip install --disable-pip-version-check -q -r $requirements
    if ($LASTEXITCODE -ne 0) { Stop-WithError '安装依赖失败，请检查网络连接后重试。' }
    [IO.File]::WriteAllText($requirementsStamp, $hash)
}

# ---------- 3. 启动 ----------
& $venvPython -m obs_overlay @args
exit $LASTEXITCODE
