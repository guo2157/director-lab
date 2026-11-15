<#
.SYNOPSIS
  DirectorLab 一键下载 ComfyUI（Windows 免安装版）+ 全部必需模型。

.DESCRIPTION
  在 Windows 10/11 64 位上执行，做三件事：
    1. 下载 ComfyUI Windows 便携版并解压（自带 Python 与 CUDA 依赖，无需装环境）
    2. 按项目工作流清单下载 Qwen-Image-2.1 / MiniMax-H3 / Qwen3-TTS 模型到对应目录
    3. 安装 Qwen3-TTS 自定义节点（缺了它 TTS 工作流会报 missing_node_type）

  下载完成后整个 InstallDir 可以直接压缩上传网盘发给买家。
  单个大文件支持断点续传，中断后重新运行同一命令即可继续。

.PARAMETER InstallDir
  安装目录，默认脚本所在目录下的 ComfyUI_windows_portable。

.PARAMETER Groups
  要下载的模型组，逗号分隔：h3,qwen,tts,h3speed,h3vram 或 all。
    h3       MiniMax H3 视频模型 + 文本编码器 + 视频/音频 VAE（约 54 GB）
    qwen     Qwen-Image-2.1 Turbo 图像模型（约 17 GB）
    tts      Qwen3-TTS 1.7B 声音模型（约 4 GB）
    h3speed  H3 提速件：int8 VAE + 4 步 Turbo LoRA（约 4.8 GB）
    h3vram   H3 省显存文本编码器 nvfp4（约 15.7 GB，替代 int8 版）

.PARAMETER Source
  下载源：auto（默认，先官方后镜像）/ official / mirror（hf-mirror.com）。

.PARAMETER SkipComfy
  跳过 ComfyUI 本体，只下模型。

.PARAMETER SkipModels
  跳过模型，只装 ComfyUI。

.PARAMETER DryRun
  只打印将要下载的地址和体积，不实际下载。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup_comfyui_windows.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup_comfyui_windows.ps1 -InstallDir D:\ComfyUI -Groups h3,qwen,tts,h3speed
#>

[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string[]]$Groups = @('h3', 'qwen', 'tts'),
    [ValidateSet('auto', 'official', 'mirror', 'modelscope')]
    [string]$Source = 'auto',
    [switch]$SkipComfy,
    [switch]$SkipModels,
    [switch]$DryRun,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 通过 .bat 调用时 param 默认值里的 $PSScriptRoot 可能为空，这里兜底
if (-not $InstallDir) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $scriptDir) { $scriptDir = $PWD.Path }
    $InstallDir = Join-Path $scriptDir 'ComfyUI_windows_portable'
}

# ---------------------------------------------------------------------------
# 模型清单：repo/path 与 ComfyUI/models 下的相对路径一致
# 与 backend/data/workflows/*.json 里的文件名逐一对齐，改名前先改工作流
# ---------------------------------------------------------------------------
$ModelList = @(
    @{ Group = 'h3'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors'; Size = 20970379616; Note = 'H3 Ref2VA 扩散模型 INT8' },
    @{ Group = 'h3'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors'; Size = 27141342152; Note = 'Qwen3-VL-32B 文本编码器 INT8' },
    @{ Group = 'h3'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'vae/minimax_h3_video_vae_fp16.safetensors'; Size = 5207808496; Note = 'H3 视频 VAE FP16' },
    @{ Group = 'h3'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'vae/minimax_h3_audio_vae_fp32.safetensors'; Size = 605254808; Note = 'H3 音频 VAE FP32' },
    @{ Group = 'qwen'; Repo = 'Comfy-Org/Qwen-Image-2.1'; Path = 'diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors'; Size = 7256787829; Note = 'Qwen-Image-2.1 Turbo 扩散模型 INT8' },
    @{ Group = 'qwen'; Repo = 'Comfy-Org/Qwen-Image-2.1'; Path = 'text_encoders/qwen3vl_8b_int8_convrot.safetensors'; Size = 9350798360; Note = 'Qwen3-VL-8B 文本编码器 INT8' },
    @{ Group = 'qwen'; Repo = 'Comfy-Org/Qwen-Image-2.1'; Path = 'vae/qwen_image_2.1_vae_bf16.safetensors'; Size = 675509688; Note = 'Qwen-Image-2.1 VAE BF16' },
    @{ Group = 'h3speed'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'vae/minimax_h3_video_vae_int8_convrot.safetensors'; Size = 2811065184; Note = 'H3 视频 VAE INT8' },
    @{ Group = 'h3speed'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors'; Size = 1956193000; Note = 'H3 Ref2V 4 步 Turbo LoRA' },
    @{ Group = 'h3vram'; Repo = 'Comfy-Org/MiniMax-H3'; Path = 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'; Size = 15687142551; Note = 'Qwen3-VL-32B 文本编码器 NVFP4' }
)

$TtsRepo = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
$TtsNodeRepo = 'DarioFT/ComfyUI-Qwen3-TTS'
$HfMirror = 'https://hf-mirror.com'
$GhProxies = @('', 'https://ghfast.top/', 'https://gh-proxy.com/')

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Gray
}

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Get-CurlPath {
    $candidates = @(
        (Join-Path $env:SystemRoot 'System32\curl.exe'),
        (Join-Path $env:SystemRoot 'SysWOW64\curl.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw ' 找不到 curl.exe。Windows 10 1803 之后自带，请先升级系统或手动安装 curl。'
}

function Test-Url([string]$Url) {
    $curl = Get-CurlPath
    $code = & $curl -s -o NUL -I -L --connect-timeout 15 --max-time 40 -w '%{http_code}' $Url 2>$null
    return ($code -match '^2')
}

function Get-HfEndpoint {
    if ($Source -eq 'official') { return 'https://huggingface.co' }
    if ($Source -eq 'mirror') { return $HfMirror }
    if (Test-Url 'https://huggingface.co/api/models?limit=1') { return 'https://huggingface.co' }
    Write-Info 'huggingface.co 不可达，改用镜像 hf-mirror.com'
    return $HfMirror
}

function Invoke-Download([string]$Url, [string]$OutFile, [string]$Label) {
    $dir = Split-Path -Parent $OutFile
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $curl = Get-CurlPath
    Write-Info "下载 $Label"
    Write-Info "  -> $OutFile"
    & $curl -L -C - --retry 5 --retry-delay 5 --connect-timeout 20 -o $OutFile $Url
    if ($LASTEXITCODE -ne 0) {
        Write-Info "  断点续传失败（退出码 $LASTEXITCODE），尝试重新下载"
        Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
        & $curl -L --retry 5 --retry-delay 5 --connect-timeout 20 -o $OutFile $Url
        if ($LASTEXITCODE -ne 0) { throw "下载失败：$Url" }
    }
}

function Get-ModelScopeRepoFiles([string]$Repo) {
    $api = "https://www.modelscope.cn/api/v1/models/$Repo/repo/files?Revision=master"
    $j = Invoke-RestMethod -Uri $api -UseBasicParsing -TimeoutSec 60
    return @($j.Data.Files | Where-Object { $_.Type -eq 'blob' } | ForEach-Object {
        @{ Path = $_.Path; Size = [int64]$_.Size }
    })
}

function Get-ModelScopeUrl([string]$Repo, [string]$Path) {
    $enc = [uri]::EscapeDataString($Path)
    return "https://www.modelscope.cn/api/v1/models/$Repo/repo?Revision=master&FilePath=$enc"
}

function Get-ComfyPortableUrl {
    $asset = 'ComfyUI_windows_portable_nvidia.7z'
    $direct = "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/$asset"
    if (Test-Url $direct) { return $direct }
    Write-Info '直接地址不可达，尝试查询 GitHub 最新 release 资产'
    try {
        $api = Invoke-RestMethod -Uri 'https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest' -UseBasicParsing -TimeoutSec 30
        $hit = $api.assets | Where-Object { $_.name -like '*nvidia*.7z' } | Select-Object -First 1
        if ($hit) { return $hit.browser_download_url }
    } catch {
        Write-Info "查询 release 失败：$($_.Exception.Message)"
    }
    return $direct
}

function Get-7zr {
    $dest = Join-Path $env:TEMP '7zr.exe'
    if (Test-Path $dest) { return $dest }
    Write-Info '下载 7zr.exe（用于解压 ComfyUI 便携包）'
    Invoke-Download 'https://www.7-zip.org/a/7zr.exe' $dest '7zr.exe'
    return $dest
}

function Test-FreeSpace([string]$Path, [double]$NeedBytes) {
    $dir = $Path
    while (-not (Test-Path $dir)) {
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    $drive = (Get-Item $dir).PSDrive
    $free = $drive.Free
    Write-Info ("目标盘剩余空间：{0}，预计需要：{1}" -f (Format-Size $free), (Format-Size $NeedBytes))
    if ($free -lt $NeedBytes) {
        throw "磁盘空间不足：剩余 $(Format-Size $free)，至少需要 $(Format-Size $NeedBytes)。可用 -InstallDir 指到更大的盘。"
    }
}

# ---------------------------------------------------------------------------
# 0. 环境检查
# ---------------------------------------------------------------------------
Write-Host 'DirectorLab —— ComfyUI 与模型一键下载（Windows）' -ForegroundColor Green
Write-Info "安装目录：$InstallDir"
Write-Info "模型组：$($Groups -join ', ')"

if ($Groups -contains 'all') { $Groups = @('h3', 'qwen', 'tts', 'h3speed', 'h3vram') }
$unknown = $Groups | Where-Object { @('h3', 'qwen', 'tts', 'h3speed', 'h3vram') -notcontains $_ }
if ($unknown) { throw "未知模型组：$($unknown -join ', ')" }

$picked = $ModelList | Where-Object { $Groups -contains $_.Group }
$needBytes = 0
foreach ($m in $picked) { $needBytes += $m.Size }
$needBytes += 6GB   # ComfyUI 本体解压后约 3-4 GB，留余量
$needBytes += 4GB   # TTS 仓库与临时文件

if (-not $DryRun) { Test-FreeSpace -Path $InstallDir -NeedBytes $needBytes }

$hf = Get-HfEndpoint
Write-Info "HuggingFace 源：$hf"

# ---------------------------------------------------------------------------
# 1. ComfyUI 本体
# ---------------------------------------------------------------------------
$parentDir = Split-Path -Parent $InstallDir
if (-not $parentDir) { $parentDir = $PWD.Path }
$comfyRoot = Join-Path $parentDir 'ComfyUI_windows_portable'
$portableBat = Join-Path $comfyRoot 'run_nvidia_gpu.bat'

if ($SkipComfy) {
    Write-Step '跳过 ComfyUI 本体'
} elseif (Test-Path $portableBat) {
    Write-Step "已存在 ComfyUI：$comfyRoot，跳过下载"
} else {
    Write-Step '下载 ComfyUI Windows 便携版（约 1.5 GB）'
    $url = Get-ComfyPortableUrl
    $archive = Join-Path $parentDir 'ComfyUI_windows_portable_nvidia.7z'
    if (-not $DryRun) {
        Invoke-Download $url $archive 'ComfyUI 便携版'
        $sevenZip = Get-7zr
        Write-Step '解压 ComfyUI'
        & $sevenZip x $archive "-o$parentDir" -y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'ComfyUI 解压失败' }
        if (-not (Test-Path $portableBat)) { throw "解压后未找到 $portableBat，请检查压缩包结构" }
    } else {
        Write-Info "（DryRun）$url -> $archive"
    }
}

$modelsDir = Join-Path $comfyRoot 'ComfyUI\models'
$customNodesDir = Join-Path $comfyRoot 'ComfyUI\custom_nodes'

# ---------------------------------------------------------------------------
# 2. 模型
# ---------------------------------------------------------------------------
if ($SkipModels) {
    Write-Step '跳过模型下载'
} else {
    foreach ($m in $picked) {
        $target = Join-Path $modelsDir ($m.Path -replace '/', '\')
        $name = Split-Path -Leaf $target
        $url = "$hf/$($m.Repo)/resolve/main/$($m.Path)?download=true"
        $sizeText = Format-Size $m.Size
        if ($DryRun) {
            Write-Info "[$($m.Group)] $name ($sizeText)  $url"
            continue
        }
        if ((Test-Path $target) -and ((Get-Item $target).Length -eq $m.Size)) {
            Write-Info "[跳过] $name 已完整（$sizeText）"
            continue
        }
        Write-Step "[$($m.Group)] $($m.Note) —— $sizeText"
        Invoke-Download $url $target $name
        $actual = (Get-Item $target).Length
        if ($actual -ne $m.Size) {
            Write-Info "  体积不符：期望 $($m.Size) 字节，实际 $actual 字节，可能是镜像返回了错误页"
            if ($actual -lt 1MB) {
                Remove-Item $target -Force
                throw "下载失败：$url 返回内容不是模型文件（可能是 401/404）。官方仓库若需登录，请先配置 HF_TOKEN 或改 -Source mirror。"
            }
        }
    }

    # Qwen3-TTS 整个仓库（多文件；ModelScope 国内速度明显更快，优先用它）
    if ($Groups -contains 'tts') {
        Write-Step 'Qwen3-TTS 声音模型（约 4 GB）'
        $ttsDir = Join-Path $modelsDir 'Qwen3-TTS\Qwen3-TTS-12Hz-1.7B-CustomVoice'
        $files = @()
        $useMs = ($Source -eq 'auto' -or $Source -eq 'modelscope')
        if ($useMs) {
            try {
                $msFiles = Get-ModelScopeRepoFiles $TtsRepo
                if ($msFiles.Count -ge 5) {
                    $files = @($msFiles | Where-Object { $_.Path -notmatch '(^|/)\.' -and $_.Path -notmatch '\.(md|gitattributes)$' })
                    Write-Info "文件列表来源：ModelScope（实测约 8-9 MB/s）"
                } else {
                    Write-Info 'ModelScope 仓库文件不完整，改用 HuggingFace 源'
                    $useMs = $false
                }
            } catch {
                Write-Info "ModelScope 列表获取失败：$($_.Exception.Message)"
                $useMs = $false
            }
        }
        if (-not $files -or $files.Count -eq 0) {
            $useMs = $false
            $apiUrl = "$hf/api/models/$TtsRepo"
            try {
                $meta = Invoke-RestMethod -Uri $apiUrl -UseBasicParsing -TimeoutSec 60
                $files = @($meta.siblings | ForEach-Object { $_.rfilename } | Where-Object {
                    $_ -and $_ -notmatch '(^|/)\.' -and $_ -notmatch '\.(md|gitattributes)$'
                } | ForEach-Object { @{ Path = $_; Size = [int64]0 } })
            } catch {
                throw "获取 Qwen3-TTS 文件列表失败（$apiUrl）：$($_.Exception.Message)"
            }
            if (-not $files -or $files.Count -eq 0) { throw 'Qwen3-TTS 文件列表为空，请改用 -Source mirror 重试' }
            Write-Info "文件列表来源：$hf"
        }
        foreach ($f in $files) {
            $u = if ($useMs) { Get-ModelScopeUrl $TtsRepo $f.Path } else { "$hf/$TtsRepo/resolve/main/$($f.Path)?download=true" }
            if ($DryRun) {
                Write-Info ("[tts] {0} ({1})  {2}" -f $f.Path, (Format-Size $f.Size), $u)
                continue
            }
            $out = Join-Path $ttsDir ($f.Path -replace '/', '\')
            if (Test-Path $out) {
                $len = (Get-Item $out).Length
                if (($f.Size -gt 0 -and $len -eq $f.Size) -or ($f.Size -eq 0 -and $len -gt 0)) {
                    Write-Info "[跳过] $($f.Path)"
                    continue
                }
            }
            Invoke-Download $u $out $f.Path
            if ($f.Size -gt 0) {
                $actual = (Get-Item $out).Length
                if ($actual -ne $f.Size) {
                    Write-Info "  体积不符：期望 $($f.Size) 字节，实际 $actual 字节"
                    if ($actual -lt 1MB) { Remove-Item $out -Force; throw "下载失败：$u" }
                }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 3. Qwen3-TTS 自定义节点
# ---------------------------------------------------------------------------
if (-not $SkipComfy -and ($Groups -contains 'tts')) {
    $nodeDir = Join-Path $customNodesDir 'ComfyUI-Qwen3-TTS'
    if ($DryRun) {
        Write-Info "[node] $TtsNodeRepo -> $nodeDir"
    } elseif (Test-Path (Join-Path $nodeDir 'pyproject.toml')) {
        Write-Step 'Qwen3-TTS 节点已存在，跳过'
    } else {
        Write-Step '安装 Qwen3-TTS 自定义节点'
        $zip = Join-Path $env:TEMP 'ComfyUI-Qwen3-TTS.zip'
        $ok = $false
        foreach ($p in $GhProxies) {
            $u = "$($p)https://codeload.github.com/$TtsNodeRepo/zip/refs/heads/main"
            try {
                Invoke-Download $u $zip 'ComfyUI-Qwen3-TTS 源码'
                $ok = $true
                break
            } catch {
                Write-Info "  源失败：$u"
            }
        }
        if (-not $ok) { throw 'Qwen3-TTS 节点下载失败，请检查网络' }
        $tmp = Join-Path $env:TEMP ('qwen3tts_' + [guid]::NewGuid().ToString('N'))
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $inner) { throw '自定义节点压缩包结构异常' }
        if (Test-Path $nodeDir) { Remove-Item $nodeDir -Recurse -Force }
        Move-Item $inner.FullName $nodeDir
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Write-Info "已安装到 $nodeDir"
        Write-Info '首次启动 ComfyUI 时它会自动安装自身依赖，需要联网'
    }
}

# ---------------------------------------------------------------------------
# 4. 收尾
# ---------------------------------------------------------------------------
Write-Step '完成'
Write-Info "ComfyUI 目录：$comfyRoot"
Write-Info '启动方式：双击 run_nvidia_gpu.bat，浏览器打开 http://127.0.0.1:8188'
Write-Info '在 DirectorLab 设置页把 ComfyUI 地址填成 http://127.0.0.1:8188 并点「测试连接」'
Write-Info ''
Write-Info '打包给买家：整个 ComfyUI_windows_portable 文件夹压缩后上传网盘即可。'
Write-Info '建议用 7-Zip 的「仅存储」模式（模型文件本身已压缩，再压缩几乎不省空间还费时间）。'
Write-Info ''
Write-Info '未包含（需手动获取，涉及第三方许可证）：'
Write-Info '  · stable-audio-open-1.0.safetensors  -> ComfyUI/models/checkpoints'
Write-Info '  · t5-base.safetensors                -> ComfyUI/models/clip'
Write-Info '  这两个仅 BGM 工作流使用，不下也能出片（只影响背景音乐生成）。'

if (-not $NoPause -and -not $DryRun) {
    Write-Host ''
    Read-Host '按回车退出'
}
