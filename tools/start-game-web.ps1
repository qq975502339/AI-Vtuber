# 直播画面：把打包好的 web-desktop 用本地 HTTP 服务起来，并打开带 ?ws= 的页面。
#
# 只做这一件事：起静态服务 + 开浏览器。不启动 Luna、不改任何配置。
# 关窗口或 Ctrl+C 时会把静态服务一起关掉。
#
# 用法（默认猜笔画 g2）：
#   .\start-game-web.ps1
#   .\start-game-web.ps1 -Game g1          # 看图猜成语
#   .\start-game-web.ps1 -Port 8090        # 换端口
#   .\start-game-web.ps1 -NoBrowser        # 只起服务，自己开浏览器
#
# 为什么必须用 HTTP：Cocos 的 web 包走 ES module + wasm，用 file:// 直接打开会因为
# 模块/跨域限制白屏；而且 ?ws= 里的地址只有浏览器里才读得到。

param(
    [ValidateSet('g1', 'g2')] [string]$Game = 'g2',
    [int]$Port = 8080,
    [string]$BuildDir = 'C:\Users\97550\Desktop\大鹅抓成语AI\build\web-desktop',
    [string]$WsUrl = 'ws://127.0.0.1:9001',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

function Test-Port([int]$p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $ok = $c.BeginConnect('127.0.0.1', $p, $null, $null).AsyncWaitHandle.WaitOne(600)
        $c.Close()
        return [bool]$ok
    } catch { return $false }
}

# ---- 1) 与产物对账 ----
if (-not (Test-Path (Join-Path $BuildDir 'index.html'))) {
    Write-Host "找不到 $BuildDir\index.html" -ForegroundColor Red
    Write-Host "先在 Cocos Creator 里：构建发布 → 平台选 Web Desktop → 构建。" -ForegroundColor Yellow
    Write-Host "（改了 assets/**/*.ts 就必须重新构建，产物不会自动跟随代码）" -ForegroundColor Yellow
    exit 1
}

# ---- 2) 端口已占：多半是上次没关，直接复用 ----
$server = $null
$reused = $false
if (Test-Port $Port) {
    $reused = $true
    Write-Host "端口 $Port 已在监听，直接复用（上次的静态服务没关？）" -ForegroundColor Yellow
} else {
    $py = $null
    foreach ($cand in @('python', 'py')) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
    }
    if (-not $py) {
        Write-Host "没找到 python，装一个或把 python 加进 PATH" -ForegroundColor Red
        exit 1
    }
    $argList = @('-m', 'http.server', "$Port", '--directory', "`"$BuildDir`"")
    if ($py -eq 'py') { $argList = @('-3') + $argList }
    $server = Start-Process -FilePath $py -ArgumentList $argList -PassThru -WindowStyle Hidden
    Write-Host "静态服务已启动（PID $($server.Id)）：$BuildDir → http://127.0.0.1:$Port" -ForegroundColor Green

    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Port $Port) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Port $Port)) {
        Write-Host "静态服务 5 秒内没起来，看看 $py 是不是被拦了" -ForegroundColor Red
        exit 1
    }
}

# ---- 3) 体检：游戏桥在不在（不在的话游戏会静默退回「本地假 AI」）----
if (Test-Port 9001) {
    Write-Host "游戏桥 9001 在听" -ForegroundColor Green
} else {
    Write-Host "[注意] 游戏桥 9001 没在听：页面会退回「本地假 AI」，游戏会自己跟自己玩，看着像接上了但其实没接。" -ForegroundColor Yellow
    Write-Host "  先把 Luna 起起来（main.py 会同时开 5001 和 9001）。" -ForegroundColor Yellow
}

# ---- 4) 开浏览器 ----
$url = 'http://127.0.0.1:{0}/index.html?{1}=1&ws={2}' -f $Port, $Game, $WsUrl
Write-Host "打开：$url" -ForegroundColor Cyan
if (-not $NoBrowser) { Start-Process $url }

Write-Host ""
Write-Host "这个窗口别关（关掉静态服务就停了）。Ctrl+C 结束。" -ForegroundColor Gray
try {
    while ($true) {
        Start-Sleep -Seconds 2
        if ($server -and $server.HasExited) {
            Write-Host "静态服务意外退出（退出码 $($server.ExitCode)）" -ForegroundColor Red
            exit 1
        }
    }
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        Write-Host "静态服务已关闭" -ForegroundColor Gray
    } elseif ($reused) {
        Write-Host "静态服务是复用的，没有关闭它" -ForegroundColor Gray
    }
}