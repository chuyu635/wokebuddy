# WorkBuddy 调试模式启动器
# 作用：用 --remote-debugging-port 参数启动客户端，之后就能在浏览器里看它的网络请求。
# 只在抓 token 时用一次，平时照常双击原来的快捷方式即可。

$ErrorActionPreference = 'Stop'
$exe  = 'D:\wokebuddy\WorkBuddy\WorkBuddy.exe'
$port = 9222

Write-Host ''
Write-Host '=== WorkBuddy 调试模式启动器 ===' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $exe)) {
    Write-Host "找不到客户端：$exe" -ForegroundColor Red
    Read-Host '按回车关闭' | Out-Null
    exit 1
}

# 调试端口只在「进程启动时」生效，所以必须先完全退出已开着的客户端
$procs = Get-Process -Name 'WorkBuddy' -ErrorAction SilentlyContinue
if ($procs) {
    Write-Host '检测到 WorkBuddy 正在运行。' -ForegroundColor Yellow
    Write-Host '请完全退出它（右上角关闭不够，要到右下角托盘图标右键 -> 退出）。'
    Read-Host '退出后按回车继续' | Out-Null

    $procs = Get-Process -Name 'WorkBuddy' -ErrorAction SilentlyContinue
    if ($procs) {
        $ans = Read-Host '还在运行，要我强制结束它吗？(y/N)'
        if ($ans -eq 'y') {
            $procs | Stop-Process -Force
            Start-Sleep -Seconds 2
        } else {
            Write-Host '已取消，什么都没做。' -ForegroundColor Yellow
            Read-Host '按回车关闭' | Out-Null
            exit 1
        }
    }
}

Write-Host '正在以调试模式启动...' -ForegroundColor Gray
Start-Process -FilePath $exe -ArgumentList "--remote-debugging-port=$port"

# 轮询等待端口就绪
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $port)
        $client.Close()
        $ready = $true
        break
    } catch { }
}

Write-Host ''
if ($ready) {
    Write-Host "成功：调试端口已就绪  http://127.0.0.1:$port" -ForegroundColor Green
    Write-Host ''
    Write-Host '接下来：' -ForegroundColor Cyan
    Write-Host '  1. 浏览器会打开一个页面，点里面 WorkBuddy 那一条，就是它的开发者工具'
    Write-Host '  2. 如果点不开，地址栏输入  edge://inspect  （Chrome 就输 chrome://inspect）'
    Write-Host '     点 Configure... 加上 localhost:9222，等下面冒出 WorkBuddy 页面，点 inspect'
    Write-Host '  3. 打开后切到 Network，勾 Keep log，Filter 输 meter'
    Write-Host '  4. 回客户端点一次签到'
    Write-Host '  5. 右键那条请求 -> Copy -> Copy as cURL (bash)'
    Write-Host ''
    Start-Process "http://127.0.0.1:$port"
} else {
    Write-Host '端口没起来，这个版本可能禁用了远程调试。' -ForegroundColor Red
    Write-Host '把这段输出截图发我，我走另外一条路（直接从本地数据里取登录态）。'
}

Write-Host ''
Read-Host '按回车关闭本窗口' | Out-Null
