# 构建 Director Lab Windows 单文件 exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
param(
    [switch]$SkipFrontend
)

# 允许原生命令输出到 stderr（PS5.1 下会被当作终止错误），改为手动检查退出码
$ErrorActionPreference = "Continue"

$Repo = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Repo "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "未找到 $Python，请先创建 backend/.venv 并安装依赖"
}

if (-not $SkipFrontend) {
    Write-Host "[1/4] 构建前端 ..."
    Push-Location (Join-Path $Repo "frontend")
    try {
        npm run build
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path (Join-Path $Repo "frontend\dist\index.html"))) {
    throw "frontend/dist 不存在，前端构建失败"
}

Write-Host "[2/4] 收集 ffmpeg / ffprobe ..."
$BinDir = Join-Path $Backend "packaging\bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
foreach ($name in @("ffmpeg.exe", "ffprobe.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) {
        Copy-Item $cmd.Source (Join-Path $BinDir $name) -Force
        Write-Host "  + $name <- $($cmd.Source)"
    } else {
        Write-Warning "PATH 中未找到 $name，导出/合成功能将依赖目标机器自行安装"
    }
}

Write-Host "[3/4] 准备 PyInstaller ..."
$HasPyInstaller = $false
try {
    & $Python -m PyInstaller --version *> $null
    $HasPyInstaller = ($LASTEXITCODE -eq 0)
} catch {}
if (-not $HasPyInstaller) {
    $Uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($Uv) {
        & uv pip install --python $Python pyinstaller
    } else {
        & $Python -m pip install --upgrade pyinstaller
    }
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }
}

Write-Host "[4/4] 打包 exe（首次约需数分钟）..."
$DistPath = Join-Path $Repo "dist_desktop"
$WorkPath = Join-Path $Repo "build\desktop"
& $Python -m PyInstaller --noconfirm --clean `
    --distpath $DistPath --workpath $WorkPath `
    (Join-Path $Backend "directorlab.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$ExeName = if ($env:DLAB_DESKTOP_CONSOLE -eq "1") { "DirectorLabConsole.exe" } else { "DirectorLab.exe" }
$Exe = Join-Path $DistPath $ExeName

Write-Host "[5/5] 拷贝使用手册与提示词模板 ..."
$DocsDir = Join-Path $Repo "sales\xianyu\delivery"
if (Test-Path $DocsDir) {
    Get-ChildItem $DocsDir -File | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $DistPath $_.Name) -Force
        Write-Host "  + $($_.Name)"
    }
} else {
    Write-Warning "未找到 $DocsDir，跳过手册与模板"
}

Write-Host ""
Write-Host "构建完成：$Exe"
Write-Host "首次运行会在 %APPDATA%\DirectorLab 下创建数据库与媒体目录，未配置 ComfyUI 时自动打开设置页。"
