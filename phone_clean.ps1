#Requires -Version 5.1
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

$pkgs = @(
    @{p='com.miui.systemAdSolution';                l='系统广告'}
    @{p='com.miui.carlink';                         l='车联服务'}
    @{p='com.milink.service';                       l='投屏服务'}
    @{p='com.miui.cloudservice';                    l='小米云服务'}
    @{p='com.miui.cloudbackup';                     l='云备份'}
    @{p='com.miui.micloudsync';                     l='云同步'}
    @{p='com.xiaomi.micloud.sdk';                   l='云服务SDK'}
    @{p='com.xiaomi.payment';                       l='小米支付'}
    @{p='com.xiaomi.simactivate.service';            l='SIM激活'}
    @{p='com.miui.analytics';                       l='Analytics'}
    @{p='com.miui.msa.global';                      l='MSA广告'}
    @{p='com.miui.daemon';                          l='系统守护'}
    @{p='com.miui.hybrid';                          l='Hybrid'}
    @{p='com.miui.player';                          l='音乐'}
    @{p='com.miui.video';                           l='视频'}
    @{p='com.miui.fm';                              l='收音机'}
    @{p='com.miui.screenrecorder';                  l='屏幕录制'}
    @{p='com.miui.cleanmaster';                     l='垃圾清理'}
    @{p='com.miui.powerkeeper';                     l='电量性能'}
)

$ast = @(
    @{p='com.tencent.mm';                   l='微信'}
    @{p='com.tencent.mobileqq';             l='QQ'}
    @{p='com.taobao.taobao';                l='手机淘宝'}
    @{p='com.jingdong.app.mall';            l='京东'}
    @{p='com.alibaba.android.rimet';        l='钉钉'}
    @{p='com.eg.android.AlipayGphone';      l='支付宝'}
)

$refreshApps = @(
    'com.ss.android.ugc.aweme'         # 抖音
    'com.taobao.taobao'                # 淘宝
    'tv.danmaku.bili'                  # B站
    'com.jingdong.app.mall'            # 京东
    'com.xunmeng.pinduoduo'            # 拼多多
    'com.android.chrome'               # Chrome(购物/视频网页)
    'com.tencent.qqlive'               # 腾讯视频
    'com.youku.phone'                  # 优酷
    'com.qiyi.video'                   # 爱奇艺
    'com.smile.gifmaker'               # 快手
    'com.autonavi.minimap'             # 高德地图
)

$g_model = ''; $g_sn = ''

function Dev-Chk {
    Write-Host '正在连接手机...'
    & adb start-server 2>$null | Out-Null
    $r = & adb devices 2>$null
    $lines = @($r -split "`n" | Where-Object { $_ -match '\S' })
    $hasDev = $false
    foreach ($ln in $lines) {
        if ($ln -match 'device$' -and $ln -notmatch 'List of') {
            Write-Host "   已连接: $($ln.Trim())"
            $hasDev = $true
            break
        }
    }
    if (-not $hasDev) {
        Write-Host '未检测到设备，请连接并开启USB调试后按回车重试，或按Q退出'
        do {
            $k = $host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown').Character
            if ($k -eq 'q' -or $k -eq 'Q') { return $false }
            if ($k -eq 13) {
                Write-Host '正在重试...'
                $r = & adb devices 2>$null
                $lines = @($r -split "`n" | Where-Object { $_ -match '\S' })
                foreach ($ln in $lines) {
                    if ($ln -match 'device$' -and $ln -notmatch 'List of') {
                        Write-Host "   已连接: $($ln.Trim())"
                        $hasDev = $true
                        break
                    }
                }
                if ($hasDev) { break }
                Write-Host '仍未检测到设备，按回车重试，或按Q退出'
            }
        } while ($true)
    }
    $script:g_model = (& adb shell getprop ro.product.model 2>$null).Trim()
    $script:g_sn = (& adb shell getprop ro.serialno 2>$null).Trim()
    if ($script:g_model) { Write-Host "   型号: $($script:g_model)" }
    if ($script:g_sn) { Write-Host "   SN: $($script:g_sn)" }
    Write-Host ''
    return $true
}

function Menu {
    Clear-Host
    Write-Host ''
    Write-Host '============================================================'
    Write-Host '   手机耗电自启动清理工具'
    Write-Host '============================================================'
    Write-Host ''
    Write-Host '  [1] 检查手机状态'
    Write-Host '  [2] 运行清理'
    Write-Host '  [3] 刷新率管理'
    Write-Host '  [Q] 退出'
    Write-Host ''
    Write-Host '============================================================'
    Write-Host ''
    do {
        if ($host.UI.RawUI.KeyAvailable) { $host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown,IncludeKeyUp') | Out-Null }
        $k = $host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown').Character
        if ($k -eq '1') { return '1' }
        if ($k -eq '2') { return '2' }
        if ($k -eq '3') { return '3' }
        if ($k -eq 'q' -or $k -eq 'Q') { return 'Q' }
    } while ($true)
}

function Do-Check {
    Clear-Host
    Write-Host '============================================================'
    Write-Host '   检查手机状态'
    Write-Host '============================================================'
    Write-Host ''
    if (-not (Dev-Chk)) { return }

    $cl = $pkgs + $ast

    Write-Host '[1/4] 检查包权限...'
    Write-Host '  正在查询...'
    $dis = & adb shell pm list packages -d 2>$null
    $bg  = & adb shell cmd appops query-op RUN_ANY_IN_BACKGROUND allow 2>$null

    Write-Host ''
    Write-Host '一、永久禁用'
    Write-Host ('  {0,-18} {1,-8}' -f '应用','状态')
    Write-Host ('  {0,-18} {1,-8}' -f '----','----')
    $i = 1
    foreach ($c in $pkgs) {
        $st = if ($dis -match $c.p) { '已禁用' } else { '未禁用' }
        Write-Host ('  {0,2}.{1,-16} {2,-8}' -f $i,$c.l,$st)
        $i++
    }
    Write-Host ''
    Write-Host '二、自启动控制'
    Write-Host ('  {0,-18} {1,-8}' -f '应用','后台')
    Write-Host ('  {0,-18} {1,-8}' -f '----','----')
    $i = 1
    foreach ($c in $ast) {
        $st = if ($bg -match $c.p) { '允许' } else { '已关闭' }
        Write-Host ('  {0,2}.{1,-16} {2,-8}' -f $i,$c.l,$st)
        $i++
    }
    Write-Host ''

    Write-Host '[2/4] 检查系统参数...'
    $va = (& adb shell settings get global window_animation_scale 2>$null).Trim()
    $vt = (& adb shell settings get global transition_animation_scale 2>$null).Trim()
    $vd = (& adb shell settings get global animator_duration_scale 2>$null).Trim()
    $vp = (& adb shell settings get system pointer_speed 2>$null).Trim()

    Write-Host ''; Write-Host '三、系统参数'
    Write-Host ('  窗口动画缩放  : {0} (目标 0.1)' -f $(if($va-eq'0.1'){'已优化'}else{$va}))
    Write-Host ('  过渡动画缩放  : {0} (目标 0.1)' -f $(if($vt-eq'0.1'){'已优化'}else{$vt}))
    Write-Host ('  动画时长缩放  : {0} (目标 0.1)' -f $(if($vd-eq'0.1'){'已优化'}else{$vd}))
    Write-Host ('  指针速度      : {0} (目标 7)' -f $(if($vp-eq'7'){'已优化'}else{$vp}))
    Write-Host ''

    Write-Host '[3/4] 息屏参数...'
    $vw = (& adb shell settings get global wifi_scan_always_enabled 2>$null).Trim()
    $vb = (& adb shell settings get global ble_scan_always_enabled 2>$null).Trim()
    $vs = (& adb shell settings get global app_standby_enabled 2>$null).Trim()
    $vm = (& adb shell settings get global mobile_data_always_on 2>$null).Trim()
    $vf = (& adb shell settings get global wifi_suspend_optimizations_enabled 2>$null).Trim()
    Write-Host ''; Write-Host '四、息屏省电'
    Write-Host ('  WiFi后台扫描   : {0} (关闭)' -f $(if($vw-eq'0'){'已优化'}else{'开启'}))
    Write-Host ('  蓝牙后台扫描   : {0} (关闭)' -f $(if($vb-eq'0'){'已优化'}else{'开启'}))
    Write-Host ('  应用待机优化   : {0} (启用)' -f $(if($vs-eq'1'){'已优化'}else{'关闭'}))
    Write-Host ('  移动数据保持   : {0} (启用)' -f $(if($vm-eq'1'){'已优化'}else{'关闭'}))
    Write-Host ('  WiFi休眠优化   : {0} (启用)' -f $(if($vf-eq'1'){'已优化'}else{'关闭'}))
    Write-Host ''

    Write-Host '[4/4] 交互参数...'
    $vh = (& adb shell settings get system haptic_feedback_enabled 2>$null).Trim()
    $vc = (& adb shell settings get system screen_off_timeout 2>$null).Trim()
    $vse = (& adb shell settings get system sound_effects_enabled 2>$null).Trim()
    $vdt = (& adb shell settings get system dtmf_tone 2>$null).Trim()
    $vbr = (& adb shell settings get system screen_brightness_mode 2>$null).Trim()
    Write-Host ''; Write-Host '五、交互优化'
    Write-Host ('  触感反馈      : {0} (关闭)' -f $(if($vh-eq'0'){'已优化'}else{'开启'}))
    Write-Host ('  屏幕超时      : {0} (1分钟)' -f $(if($vc-eq'60000'){'已优化'}else{"$($vc)ms"}))
    Write-Host ('  按键音效      : {0} (关闭)' -f $(if($vse-eq'0'){'已优化'}else{'开启'}))
    Write-Host ('  拨号按键音    : {0} (关闭)' -f $(if($vdt-eq'0'){'已优化'}else{'开启'}))
    Write-Host ('  亮度控制      : {0} (手动)' -f $(if($vbr-eq'0'){'已优化'}else{'自动'}))
    Write-Host ''

    Write-Host '============================================================'
    Write-Host '   检查完成'
    Write-Host '============================================================'
    Write-Host '按回车返回...'
    do { $k = [Console]::ReadKey($true) } while ($k.Key -ne 'Enter')
}

function Do-Clean {
    Clear-Host
    Write-Host '============================================================'
    Write-Host '   清理脚本开始'
    Write-Host '============================================================'
    Write-Host ''
    if (-not (Dev-Chk)) { return }

    Write-Host '[1/7] 记录状态...'
    $disBefore = & adb shell pm list packages -d 2>$null
    Write-Host '        OK'

    Write-Host '[2/7] 关闭自启动...'
    foreach ($a in $ast) {
        & adb shell cmd appops set $($a.p) RUN_ANY_IN_BACKGROUND ignore 2>$null
        Write-Host "       $($a.l)"
    }
    Write-Host "       共 $($ast.Count) 个"

    Write-Host '[3/7] 永久禁用...'
    foreach ($p in $pkgs) {
        & adb shell pm disable-user $($p.p) 2>$null
        Write-Host "       $($p.l)"
    }
    Write-Host "       共 $($pkgs.Count) 个"

    Write-Host '[4/7] 系统参数...'
    & adb shell settings put global window_animation_scale 0.1 2>$null
    & adb shell settings put global transition_animation_scale 0.1 2>$null
    & adb shell settings put global animator_duration_scale 0.1 2>$null
    & adb shell settings put system pointer_speed 7 2>$null
    Write-Host '       动画0.1x / 指针7'

    Write-Host '[5/7] 息屏省电...'
    & adb shell settings put global wifi_scan_always_enabled 0 2>$null
    & adb shell settings put global ble_scan_always_enabled 0 2>$null
    & adb shell settings put global app_standby_enabled 1 2>$null
    & adb shell settings put global mobile_data_always_on 1 2>$null
    & adb shell settings put global wifi_suspend_optimizations_enabled 1 2>$null
    Write-Host '       WiFi/蓝牙扫描关闭,待机优化启用'

    Write-Host '[6/7] 交互优化...'
    & adb shell settings put system haptic_feedback_enabled 0 2>$null
    & adb shell settings put system screen_off_timeout 60000 2>$null
    & adb shell settings put system sound_effects_enabled 0 2>$null
    & adb shell settings put system dtmf_tone 0 2>$null
    & adb shell settings put system screen_brightness_mode 0 2>$null
    Write-Host '       触感/音效关闭, 超时1min, 亮度手动'

    Write-Host '[7/7] 对比报告...'
    $disAfter = & adb shell pm list packages -d 2>$null
    $alr = @(); $new = @()
    foreach ($p in $pkgs) {
        $was = $disBefore -match $p.p
        $now = $disAfter -match $p.p
        if ($was) { $alr += $p.l } elseif ($now) { $new += $p.l }
    }
    Clear-Host
    Write-Host ''
    Write-Host "  ===== 清理完成 - $g_model  SN: $g_sn ====="
    Write-Host ''
    Write-Host '  关闭自启动:'
    foreach ($a in $ast) { Write-Host "    $($a.l)" }
    Write-Host ''
    Write-Host "  本次前已禁用: $($alr.Count) 个"
    if ($alr.Count -gt 0) { foreach ($i in $alr) { Write-Host "    $i" } } else { Write-Host '    (无)' }
    Write-Host ''
    Write-Host "  本次新禁用: $($new.Count) 个"
    if ($new.Count -gt 0) { foreach ($i in $new) { Write-Host "    $i" } } else { Write-Host '    (无)' }
    Write-Host ''
    Write-Host '  系统参数: 动画0.1x/指针7'
    Write-Host '  息屏省电: WiFi/蓝牙扫描关闭,待机优化启用'
    Write-Host '  交互优化: 触感/音效关闭,超时1min,亮度手动'
    Write-Host ''
    Write-Host '============================================================'
    Write-Host '按回车返回...'
    do { $k = [Console]::ReadKey($true) } while ($k.Key -ne 'Enter')
}

function Start-RefreshMon {
    if (-not $g_conn) { Write-Host '请先连接设备'; Start-Sleep 1; return }
    Stop-RefreshMon -quiet

    $pipe = [string]::Join('|', $refreshApps)
    $sh = @'
#!/system/bin/sh
PID_FILE="/data/local/tmp/refresh_mon.pid"
echo $$ > $PID_FILE
TARGETS="__PIPE__"
CUR=120
W=1080
H=2340
while [ -f $PID_FILE ]; do
    PKG=$(dumpsys activity activities 2>/dev/null | grep mFocusedApp | sed 's/.*u0 \([^/]*\).*/\1/' | head -1)
    if echo "$PKG" | grep -qE "$TARGETS"; then
        if [ "$CUR" != "60" ]; then
            cmd display set-user-preferred-display-mode $W $H 60
            CUR=60
        fi
    else
        if [ "$CUR" != "120" ]; then
            cmd display clear-user-preferred-display-mode
            CUR=120
        fi
    fi
    sleep 2
done
'@
    $sh = $sh.Replace('__PIPE__', $pipe)
    $sh = $sh.TrimStart([char]0xFEFF)

    $tmpFile = "$env:TEMP\refresh_mon.sh"
    [System.IO.File]::WriteAllText($tmpFile, $sh.Replace("`r`n","`n") + "`n", [System.Text.UTF8Encoding]::new($false))
    adb push $tmpFile /data/local/tmp/refresh_mon.sh 2>$null | Out-Null
    adb shell chmod 755 /data/local/tmp/refresh_mon.sh 2>$null | Out-Null
    adb shell "nohup sh /data/local/tmp/refresh_mon.sh > /dev/null 2>&1 &"
    Remove-Item $tmpFile -Force 2>$null
    Start-Sleep 1
    $rawMonPid = adb shell "cat /data/local/tmp/refresh_mon.pid 2>/dev/null"
    $monPid = if ($rawMonPid) { $rawMonPid.Trim() } else { '' }
    if ($monPid) {
        Write-Host "刷新率监控已启动 (PID $monPid)"
        Write-Host "目标应用: $($refreshApps.Count) 个"
    } else {
        Write-Host '启动失败'
    }
}

function Stop-RefreshMon {
    param([switch]$quiet)
    $rawMonPid = adb shell "cat /data/local/tmp/refresh_mon.pid 2>/dev/null"
    $monPid = if ($rawMonPid) { $rawMonPid.Trim() } else { '' }
    if ($monPid) {
        adb shell "rm /data/local/tmp/refresh_mon.pid; kill $monPid 2>/dev/null" 2>$null | Out-Null
        if (-not $quiet) { Write-Host "已停止监控 (PID $monPid)" }
    } elseif (-not $quiet) {
        Write-Host '监控未在运行'
    }
    adb shell cmd display clear-user-preferred-display-mode 2>$null | Out-Null
}

function Do-Refresh {
    Clear-Host
    Write-Host ''
    Write-Host '============================================================'
    Write-Host '   刷新率管理'
    Write-Host '============================================================'
    Write-Host ''
    if (-not (Dev-Chk)) { return }

    $rawMonPid = adb shell "cat /data/local/tmp/refresh_mon.pid 2>/dev/null"
    $monPid = if ($rawMonPid) { $rawMonPid.Trim() } else { '' }
    if ($monPid) {
        Write-Host '  状态: 运行中'
        $cur = (adb shell cmd display get-user-preferred-display-mode 2>$null | Select-String '60' -Quiet)
        Write-Host ('  当前刷新率: {0}Hz' -f $(if($cur){'60'}else{'120'}))
    } else {
        Write-Host '  状态: 未运行'
    }
    Write-Host ''
    Write-Host '  目标应用:'
    foreach ($a in $refreshApps) { Write-Host "    $a" }
    Write-Host ''
    Write-Host '  [1] 启动监控'
    Write-Host '  [S] 停止监控'
    Write-Host '  [B] 返回主菜单'
    Write-Host ''
    Write-Host '============================================================'
    Write-Host ''
    do {
        while ([Console]::KeyAvailable) { [Console]::ReadKey($true) | Out-Null }
        $k = [Console]::ReadKey($true).KeyChar
        if ($k -eq '1') { Start-RefreshMon; break }
        if ($k -eq 's' -or $k -eq 'S') { Stop-RefreshMon; break }
        if ($k -eq 'b' -or $k -eq 'B') { break }
    } while ($true)
    Write-Host ''; Write-Host '按回车返回...'; do { $k = [Console]::ReadKey($true) } while ($k.Key -ne 'Enter')
}

# 启动时检查设备
Clear-Host
Write-Host ''
Write-Host '============================================================'
Write-Host '   手机耗电自启动清理工具'
Write-Host '============================================================'
Write-Host ''
& adb start-server 2>$null | Out-Null
$r = & adb devices 2>$null
$lines = @($r -split "`n" | Where-Object { $_ -match '\S' })
$g_conn = $false
foreach ($ln in $lines) {
    if ($ln -match 'device$' -and $ln -notmatch 'List of') {
        Write-Host "   已连接: $($ln.Trim())"
        $g_conn = $true
        $g_model = (& adb shell getprop ro.product.model 2>$null).Trim()
        $g_sn = (& adb shell getprop ro.serialno 2>$null).Trim()
        if ($g_model) { Write-Host "   型号: $g_model" }
        if ($g_sn) { Write-Host "   SN: $g_sn" }
        break
    }
}
if (-not $g_conn) { Write-Host '   未连接设备' }
Write-Host ''
Write-Host '  [1] 检查手机状态'
Write-Host '  [2] 运行清理'
Write-Host '  [3] 刷新率管理'
Write-Host '  [Q] 退出'
Write-Host ''
Write-Host '============================================================'
Write-Host ''

function Show-Menu {
    Write-Host '============================================================'
    Write-Host ''
    Write-Host '  [1] 检查手机状态'
    Write-Host '  [2] 运行清理'
    Write-Host '  [3] 刷新率管理'
    Write-Host '  [Q] 退出'
    Write-Host ''
    Write-Host '============================================================'
    Write-Host ''
}

do {
    while ([Console]::KeyAvailable) { [Console]::ReadKey($true) | Out-Null }
    $k = [Console]::ReadKey($true).KeyChar
    if ($k -eq '1') { Do-Check; Show-Menu }
    elseif ($k -eq '2') { Do-Clean; Show-Menu }
    elseif ($k -eq '3') { Do-Refresh; Show-Menu }
    elseif ($k -eq 'q' -or $k -eq 'Q') { break }
} while ($true)
Clear-Host; Write-Host ''; Write-Host '再见！'; Write-Host ''
