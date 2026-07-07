# ADB 命令参考手册

> 适用于 MIUI / HyperOS，大部分命令通用 Android。标注 `[root]` 需 root 权限，标注 `[核]` 需内核支持。

---

## 一、电池与充电

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys battery` | 电量/温度/电压/充电状态/健康度 |
| `adb shell dumpsys battery set level 80` | 模拟电量（可设 0-100） |
| `adb shell dumpsys battery reset` | 恢复真实电量 |
| `adb shell cat /sys/class/power_supply/battery/current_now` | 实时电流（μA） |
| `adb shell cat /sys/class/power_supply/battery/voltage_now` | 实时电压（μV） |
| `adb shell cat /sys/class/power_supply/battery/charge_type` | 充电类型（USB/DCP/HVDCP 等） |
| `adb shell cat /sys/class/power_supply/battery/capacity` | 当前电量百分比 |
| `adb shell cat /sys/class/power_supply/battery/health` | 电池健康状态 |
| `adb shell cat /sys/class/power_supply/battery/charge_full` | 当前满电容量（μAh） |
| `adb shell cat /sys/class/power_supply/battery/charge_full_design` | 设计满电容量（μAh） |
| `adb shell cat /sys/class/power_supply/battery/cycle_count` `[核]` | 充电循环次数 |

---

## 二、温度与热管理

| 命令 | 说明 |
|---|---|
| `adb shell cat /sys/class/thermal/thermal_zone*/type` | 查看所有温区名称 |
| `adb shell cat /sys/class/thermal/thermal_zone*/temp` | 查看所有温区温度（m°C） |
| `adb shell dumpsys thermalservice` | 温控服务状态（限频/限流详情） |
| `adb shell cat /sys/class/thermal/cooling_device*/type` | 散热设备列表 |
| `adb shell cat /sys/class/thermal/cooling_device*/cur_state` | 当前散热强度 |

常见温区名称对应：
- `cpu-0-0-usr` / `cpu-1-0-usr` — CPU 各簇温度
- `gpuss-0-usr` / `gpuss-1-usr` — GPU 温度
- `xo-therm-adc` / `xo-therm-usr` — 主板/外壳温度
- `battery` — 电池温度
- `charger-therm-usr` — 充电 IC 温度
- `quiet-therm-usr` — 静默温区

---

## 三、CPU 与性能

| 命令 | 说明 |
|---|---|
| `adb shell cat /proc/cpuinfo` | CPU 型号、核心数、特性 |
| `adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` | 各核心当前频率（KHz） |
| `adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq` | 各核心最大频率 |
| `adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq` | 各核心最小频率 |
| `adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` | 各核心调度策略 |
| `adb shell cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors` | 可选调度策略 |
| `adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/stats/time_in_state` | 各频率驻留时间 |
| `adb shell cat /proc/stat` | CPU 总时间统计（user/nice/system/idle/iowait/irq/softirq） |
| `adb shell dumpsys cpuinfo` | 各进程累计 CPU 时间排行 |
| `adb shell top -b -n 1 -o %CPU -d 1` | 实时 CPU 进程快照 |
| `adb shell cat /sys/devices/system/cpu/present` | 当前可用 CPU 核心 |
| `adb shell cat /sys/devices/system/cpu/online` | 在线核心 |
| `adb shell cat /sys/devices/system/cpu/offline` | 离线核心 |
| `adb shell cat /sys/devices/system/cpu/isolated` | 隔离核心 |

---

## 四、内存与存储

| 命令 | 说明 |
|---|---|
| `adb shell cat /proc/meminfo` | 完整内存信息（总量/可用/缓存/交换等） |
| `adb shell dumpsys meminfo <pkg>` | 某应用详细内存拆解（PSS/RSS/USS/VSS） |
| `adb shell dumpsys meminfo --oom` | OOM 评级与各进程内存 |
| `adb shell cat /proc/swaps` | 交换分区信息 |
| `adb shell cat /proc/vmstat` | 虚拟内存统计 |
| `adb shell df -h` | 磁盘分区使用情况 |
| `adb shell df -h /data` | 数据分区使用情况 |
| `adb shell du -sh /data/app/*` | 各应用 APK 占用 |
| `adb shell du -sh /data/data/*` | 各应用数据占用 |
| `adb shell du -sh /sdcard/Android/data/*` | 各应用外部存储占用 |
| `adb shell sm list-disks` | 存储磁盘列表 |
| `adb shell sm list-volumes` | 卷信息 |

---

## 五、进程与线程

| 命令 | 说明 |
|---|---|
| `adb shell ps -A` | 所有进程列表 |
| `adb shell ps -A -o '%CPU,%MEM,RSS,TCNT,ARGS' --sort=-%cpu` | CPU 排行（含线程数） |
| `adb shell ps -A -o '%MEM,RSS,TCNT,ARGS' --sort=-%mem` | 内存排行（含线程数） |
| `adb shell ps -A -o PID,TCNT,ARGS` | 所有进程 PID + 线程数 |
| `adb shell ps -T -p <PID>` | 某进程所有线程 |
| `adb shell cat /proc/<PID>/status` | 进程详细信息 |
| `adb shell cat /proc/<PID>/stat` | 进程状态（含线程数 20 字段） |
| `adb shell cat /proc/<PID>/task/*/stat` | 各线程状态 |
| `adb shell cat /proc/<PID>/oom_score` | OOM 打分（高=优先被杀） |
| `adb shell cat /proc/<PID>/oom_adj` | OOM 调整值 |
| `adb shell am force-stop <pkg>` | 强杀应用 |
| `adb shell kill <PID>` | 杀进程 |
| `adb shell kill -9 <PID>` | 强制杀进程（SIGKILL） |
| `adb shell kill -3 <PID>` | 输出线程堆栈到 logcat（ANR trace） |

---

## 六、应用管理

| 命令 | 说明 |
|---|---|
| `adb shell pm list packages` | 列出所有包 |
| `adb shell pm list packages -3` | 仅第三方应用 |
| `adb shell pm list packages -s` | 仅系统应用 |
| `adb shell pm list packages -d` | 已禁用应用 |
| `adb shell pm list packages -e` | 已启用应用 |
| `adb shell pm list packages -f <关键词>` | 搜索包名 |
| `adb shell pm path <pkg>` | 显示 APK 路径 |
| `adb shell pm dump <pkg>` | 应用完整信息 |
| `adb shell pm disable <pkg>` | 禁用应用 |
| `adb shell pm enable <pkg>` | 启用应用 |
| `adb shell pm disable-user --user 0 <pkg>` | 禁用（用户空间，可恢复） |
| `adb shell pm uninstall -k --user 0 <pkg>` | 卸载预装（保留数据 `-k`） |
| `adb shell pm install -r <apk路径>` | 安装/覆盖 APK |
| `adb shell pm clear <pkg>` | 清除应用数据 |
| `adb shell pm grant <pkg> <permission>` | 授予权限 |
| `adb shell pm revoke <pkg> <permission>` | 撤销权限 |
| `adb shell dumpsys package <pkg>` | 应用安装详情（权限/组件/签名） |
| `adb shell cmd package compile -m speed -f <pkg>` | 强制 AOT 编译 |
| `adb shell cmd package compile -m speed-profile -f <pkg>` | 按 profile 编译 |
| `adb shell cmd package compile -r bg-dexopt` | 重置编译状态 |

---

## 七、Doze 与待机

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys deviceidle` | Doze 状态（深闲/浅闲/活跃/白名单） |
| `adb shell dumpsys deviceidle whitelist +<pkg>` | 加入白名单 |
| `adb shell dumpsys deviceidle whitelist -<pkg>` | 移出白名单 |
| `adb shell dumpsys deviceidle force-idle` | 强制进入 Idle 模式（调试用） |
| `adb shell dumpsys deviceidle unforce` | 退出强制 Idle |
| `adb shell dumpsys deviceidle step` | 单步推进 Doze 状态机 |
| `adb shell dumpsys deviceidle get <light|deep|force|screen|charging|network>` | 查看各约束状态 |
| `adb shell dumpsys battery unplug` | 模拟拔充电器（配合 Doze 调试） |
| `adb shell cmd appops set <pkg> RUN_IN_BACKGROUND deny` | 禁止后台运行 |
| `adb shell cmd appops set <pkg> WAKE_LOCK deny` | 禁止持有唤醒锁 |

---

## 八、唤醒锁与电源

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys power` | 电源状态 + 所有唤醒锁 |
| `adb shell cat /sys/kernel/debug/wakeup_sources` `[root/核]` | 内核唤醒源统计 |
| `adb shell dumpsys batterystats --charged` | 电池统计 |
| `adb shell dumpsys batterystats --reset` | 重置电池统计 |
| `adb shell dumpsys batterystats <pkg>` | 某应用耗电统计 |
| `adb shell dumpsys alarm` | 定时器/Alarm 列表 |
| `adb shell dumpsys activity broadcasts` | 广播接收历史 |

---

## 九、显示与刷新率

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys display` | 显示器信息（分辨率/刷新率/色域/HDR） |
| `adb shell dumpsys window displays` | 窗口显示信息 |
| `adb shell wm size` | 当前分辨率 |
| `adb shell wm density` | 当前 DPI |
| `adb shell wm size 1080x2400` | 临时修改分辨率（重启恢复） |
| `adb shell wm density 420` | 临时修改 DPI |
| `adb shell wm size reset` | 恢复分辨率 |
| `adb shell wm density reset` | 恢复 DPI |
| `adb shell wm overscan 0,0,0,100` | 屏幕边距裁剪（上右下左） |
| `adb shell settings put system screen_brightness 128` | 设置亮度（0-255） |
| `adb shell settings put system screen_off_timeout 30000` | 息屏超时（毫秒） |
| `adb shell settings get system peak_refresh_rate` | 峰值刷新率（MIUI/HyperOS） |
| `adb shell settings get system min_refresh_rate` | 最低刷新率 |
| `adb shell cmd display ab-logging-disable` | 关闭自适应亮度日志 |

**⚠️ HyperOS 刷新率注意**：MIUI 14 起封锁了 ADB 层面的刷新率动态切换。`settings put system peak_refresh_rate` 等设置会被系统忽略。仅支持全局 60Hz/120Hz 手动切换（设置 → 显示 → 屏幕刷新率）。

---

## 十、网络

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys netstats detail` | 各应用流量详情（含 UID 和标签） |
| `adb shell dumpsys wifi` | WiFi 状态、已连 SSID/BSSID、信号强度 |
| `adb shell cmd wifi set-wifi-enabled disabled` | 关闭 WiFi |
| `adb shell cmd wifi set-wifi-enabled enabled` | 开启 WiFi |
| `adb shell cmd wifi connect-network <SSID> wpa2 <密码>` | 连接 WiFi |
| `adb shell settings put global mobile_data 0` | 关闭移动数据 |
| `adb shell settings put global mobile_data 1` | 开启移动数据 |
| `adb shell settings put global airplane_mode_on 1` | 开启飞行模式 |
| `adb shell settings put global bluetooth_on 0` | 关闭蓝牙 |
| `adb shell dumpsys bluetooth_manager` | 蓝牙状态 |
| `adb shell dumpsys connectivity` | 网络连接详情 |
| `adb shell ip addr show` | 网络接口 IP |
| `adb shell ip route` | 路由表 |
| `adb shell ping -c 4 8.8.8.8` | 网络连通性测试 |
| `adb shell netstat -an` | 所有连接和监听端口 |
| `adb shell ss -tunap` | Socket 连接（含进程 PID） |

---

## 十一、日志与调试

| 命令 | 说明 |
|---|---|
| `adb logcat -b all -d` | 全量日志（一次性输出） |
| `adb logcat -b all -d > log.txt` | 导出到文件 |
| `adb logcat -v time -b all *:E` | 仅 Error 级别 |
| `adb logcat -v time -s <TAG>` | 按 TAG 过滤 |
| `adb logcat -v time | grep -i "thermal\|overheat\|hot"` | 温控相关日志 |
| `adb logcat -v time | grep -i "kill\|lowmemory\|OOM"` | 杀进程相关日志 |
| `adb logcat -b events -d` | 事件日志（Activity/唤醒等） |
| `adb logcat -b crash -d` | 崩溃日志 |
| `adb logcat -c` | 清空日志缓冲区 |
| `adb shell bugreportz` | 生成完整 bug 报告（zip） |
| `adb shell dmesg` | 内核日志（启动后） |
| `adb shell getprop` | 所有系统属性 |
| `adb shell getprop ro.build.version.release` | Android 版本 |
| `adb shell getprop ro.product.model` | 设备型号 |
| `adb shell getprop ro.product.board` | 主板代号 |
| `adb shell getprop ro.soc.manufacturer` | SoC 厂商 |
| `adb shell getprop ro.boot.hardware` | 硬件平台 |

---

## 十二、GPU 与图形

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys gfxinfo <pkg>` | 应用 GPU 渲染性能（帧率/jank） |
| `adb shell dumpsys gfxinfo <pkg> framestats` | 逐帧详细数据 |
| `adb shell dumpsys gpu <仅部分设备>` | GPU 状态 |
| `adb shell cat /sys/class/kgsl/kgsl-3d0/gpuclk` | GPU 频率（Qualcomm） |
| `adb shell cat /sys/class/kgsl/kgsl-3d0/gpubusy` | GPU 利用率（Qualcomm） |
| `adb shell settings put global force_gpu_rendering 1` | 强制 GPU 2D 渲染 |
| `adb shell settings put global hardware_ui 1` | 启用硬件 UI 加速 |

---

## 十三、传感器

| 命令 | 说明 |
|---|---|
| `adb shell dumpsys sensorservice` | 所有传感器及状态 |
| `adb shell dumpsys sensorservice | grep "active"` | 查找活跃传感器 |
| `adb shell dumpsys sensorservice | grep "wake-up"` | 查找唤醒传感器 |
| `adb shell cmd sensorservice set-sensor-state <sensor> disable` | 禁用某传感器 `[需 root]` |

---

## 十四、输入与模拟操作

| 命令 | 说明 |
|---|---|
| `adb shell input keyevent 26` | 电源键 |
| `adb shell input keyevent 3` | Home 键 |
| `adb shell input keyevent 4` | 返回键 |
| `adb shell input keyevent 24` | 音量+ |
| `adb shell input keyevent 25` | 音量- |
| `adb shell input keyevent 164` | 静音 |
| `adb shell input keyevent 82` | 菜单键 |
| `adb shell input keyevent 187` | 多任务键 |
| `adb shell input tap 500 1000` | 点击坐标 (x, y) |
| `adb shell input swipe 500 1500 500 500` | 上滑 |
| `adb shell input swipe 500 500 500 1500` | 下滑 |
| `adb shell input text "hello"` | 输入文本 |
| `adb shell input keyevent 66` | 回车键 |
| `adb shell input keyevent 67` | 退格键 |
| `adb shell input keyevent 61` | Tab 键 |

---

## 十五、系统设置（Settings）

| 命令 | 说明 |
|---|---|
| `adb shell settings list global` | 所有全局设置 |
| `adb shell settings list system` | 所有系统设置 |
| `adb shell settings list secure` | 所有安全设置 |
| `adb shell settings get global <key>` | 读全局设置值 |
| `adb shell settings put global <key> <value>` | 写全局设置 |
| `adb shell settings delete global <key>` | 删除全局设置 |
| `adb shell settings get system <key>` | 读系统设置值 |
| `adb shell settings put system <key> <value>` | 写系统设置 |
| `adb shell settings get secure <key>` | 读安全设置值 |

常用 Settings Key：

| Key | 说明 |
|---|---|
| `animator_duration_scale` | 动画速度（0=关闭） |
| `transition_animation_scale` | 过渡动画 |
| `window_animation_scale` | 窗口动画 |
| `wifi_on` | WiFi 开关 |
| `bluetooth_on` | 蓝牙开关 |
| `mobile_data` | 移动数据开关 |
| `screen_brightness` | 屏幕亮度 |
| `screen_off_timeout` | 息屏超时 |
| `accelerometer_rotation` | 自动旋转 |
| `haptic_feedback_enabled` | 触感反馈 |

---

## 十六、系统服务与组件

| 命令 | 说明 |
|---|---|
| `adb shell service list` | 所有运行中的系统服务 |
| `adb shell dumpsys -l` | 所有可 dump 的服务列表 |
| `adb shell dumpsys activity services` | 所有运行中的 Service |
| `adb shell dumpsys activity providers` | 所有 ContentProvider |
| `adb shell dumpsys activity broadcasts` | 广播接收队列 |
| `adb shell dumpsys activity intents` | 待处理 Intent |
| `adb shell svc wifi enable` | 开 WiFi（简化版） |
| `adb shell svc wifi disable` | 关 WiFi |
| `adb shell svc data enable` | 开移动数据 |
| `adb shell svc data disable` | 关移动数据 |
| `adb shell svc power stayon true` | 保持屏幕常亮 |

---

## 十七、屏幕截图与录屏

| 命令 | 说明 |
|---|---|
| `adb shell screencap -p /sdcard/screen.png` | 截图到手机 |
| `adb pull /sdcard/screen.png` | 拉取截图到电脑 |
| `adb exec-out screencap -p > screen.png` | 直接截图到电脑 |
| `adb shell screenrecord --size 1080x2400 /sdcard/video.mp4` | 录屏（Ctrl+C 停止） |
| `adb pull /sdcard/video.mp4` | 拉取录屏到电脑 |
| `adb shell screenrecord --time-limit 30 /sdcard/video.mp4` | 限时 30 秒录屏 |

---

## 十八、文件传输

| 命令 | 说明 |
|---|---|
| `adb push <本地文件> <手机路径>` | 推送到手机 |
| `adb pull <手机路径> [本地路径]` | 拉取到电脑 |
| `adb shell ls -la /sdcard/` | 列出手机目录 |
| `adb shell rm -rf /sdcard/test/` | 删除手机目录 |
| `adb shell mkdir /sdcard/test` | 创建手机目录 |
| `adb shell mv /sdcard/a.txt /sdcard/b.txt` | 移动/重命名 |

---

## 十九、重启与恢复

| 命令 | 说明 |
|---|---|
| `adb reboot` | 普通重启 |
| `adb reboot bootloader` | 重启到 Fastboot |
| `adb reboot recovery` | 重启到 Recovery |
| `adb reboot edl` | 重启到 9008 深度刷机模式（部分机型） |
| `adb shell reboot -p` | 关机 |
| `adb wait-for-device` | 等待设备连接 |
| `adb kill-server` | 关闭 ADB 服务 |
| `adb start-server` | 启动 ADB 服务 |
| `adb devices` | 列出已连接设备 |
| `adb -s <序列号> shell` | 指定设备执行 |

---

## 二十、隐藏功能与实验选项

| 命令 | 说明 |
|---|---|
| `adb shell setprop debug.hwui.renderer skiagl` | 切换到 Skia GL 渲染 |
| `adb shell setprop debug.hwui.renderer skiaglthreaded` | Skia 多线程渲染 |
| `adb shell setprop debug.hwui.renderer skiavk` | Skia Vulkan 渲染 |
| `adb shell cmd gpu <cmd>` | GPU 调试命令（部分机型） |
| `adb shell cmd stats print-stats` | 系统统计摘要 |
| `adb shell cmd shortcut set-default-launcher <pkg>` | 设置默认桌面 |

---

## 附录：本设备 HyperOS 受限清单

| 操作 | 状态 | 原因 |
|---|---|---|
| 动态切换刷新率 | 不可用 | HyperOS 封锁 `settings put` 路径 |
| 内核级 GPU 调频 | 不可用 | 需 `root` + 内核模块 |
| 卸载系统只读分区应用 | 不可用 | 只读文件系统，仅 `--user 0` 浅卸载 |
| PowerKeeper 参数调优 | 不可用 | 闭源组件，无对外配置接口 |
| 内核唤醒源追踪 | 不可用 | `/sys/kernel/debug/wakeup_sources` 需 root |
| 逐应用 CPU 限频 | 不可用 | 需 root + 内核 cgroup 调参 |
