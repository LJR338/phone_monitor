# Phone Monitor v6

> 基于 ADB 的 Android 手机实时监控仪表盘 —— 集系统监控与 App 性能测试于一体，通过浏览器访问的轻量级 Web 面板。

![Python](https://img.shields.io/badge/Python-3.7+-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 目录

- [目录结构](#目录结构)
- [功能清单](#功能清单)
- [系统架构](#系统架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [API 文档](#api-文档)
- [前端页面说明](#前端页面说明)
- [配置说明](#配置说明)
- [注意事项](#注意事项)

---

## 目录结构

```
phone_monitor/
├── phone_monitor.py            # 主程序（单文件，约 2973 行）
├── phone_monitor.bat           # Windows 启动脚本（双击运行）
├── phone_monitor_v5_backup.py  # v5 版本备份
├── ADB命令参考.md               # ADB 调试命令速查
├── phone_clean.bat             # 清理辅助脚本
├── phone_clean.ps1             # 清理辅助 PowerShell 脚本
├── .gitignore                  # Git 忽略规则
├── mode.txt                    # 当前模式（monitor / test）
├── pid.txt                     # 进程 PID
├── battery_health.json         # 电池健康数据（持久化）
├── power_save.json             # 省电策略配置
├── charge_sessions/            # 充电会话记录（JSON）
├── screenshots/                # 截图缓存
└── __pycache__/                # Python 字节码缓存
```

**项目特点**：零外部依赖，仅需 Python 标准库 + Android ADB 即可运行。

---

## 功能清单

### 一、系统监控模块（Monitor Mode）

| 功能分类 | 具体能力 | 数据来源 |
|---------|---------|---------|
| **电池** | 电量 / 温度 / 电压 / 电流 / 充电功率 / 充电类型 | `dumpsys battery` + sysfs |
| **CPU** | 整体使用率 / 逐核心 idle 率 / 核心频率 / 核心数 / TOP15 进程 | `/proc/stat` + sysfs + `ps` |
| **内存** | Total / Available / Used% / Swap% | `/proc/meminfo` |
| **存储** | /data 分区 总量 / 可用 / 使用率 | `df -h /data` |
| **温度** | 电池温度 / SoC 最高温度 | `dumpsys battery` + thermal sysfs |
| **前台应用** | 当前前台 App 包名 | `dumpsys activity` |
| **屏幕** | 亮屏/息屏状态 + 刷新率 | `dumpsys window` / `display` |
| **唤醒锁** | 活跃 Wakelock 列表 (TOP5) | `dumpsys power` |
| **唤醒源** | 唤醒源统计（次数/总时长） | `dumpsys batterystats` |

### 二、高级诊断模块

| 功能 | 说明 |
|------|------|
| **充电曲线** | 自动检测充电事件，逐秒采样电量/温度/电压/电流/功率，自动存档为 JSON |
| **内存泄漏检测** | 追踪 TOP15 进程 RSS 趋势，线性回归分析，自动告警（增长 >15% 持续 15min） |
| **电池寿命预测** | 每小时采集 charge_full 容量，持久化到 JSON，线性回归预测衰减至 80% 的剩余天数 |
| **省电策略** | 息屏时自动强杀 CPU>10% 的非白名单/非系统进程；支持白名单管理 |
| **性能模式控制** | 读写 HyperOS/MIUI 性能相关 Settings 键（power_performance / speed_mode / fixed_perf 等） |
| **系统设置优化** | 13 项耗电相关 settings 键的可视化开关 + 一键省电批量写入 |
| **GPU 监控** | 高通 Adreno / 联发科 Mali / MIUI 多路径兼容的 GPU 频率与负载采集 |
| **CPU 调度详情** | 逐核心 governor + 当前/最小/最大频率 |
| **Doze 检测** | DeviceIdle 状态（Light Idle / Deep Idle / Force Idle / 充电 / 屏幕） |
| **IO 性能** | /proc/diskstats 增量统计（读取/写入 MB） |
| **ANR 检测** | 扫描 /data/anr/ 目录，检测 traces.txt 是否增长 |

### 三、App 测试模块（Test Mode）

| 功能 | 说明 | 数据来源 |
|------|------|---------|
| **冷启动测试** | am force-stop → am start -W，记录 TotalTime / WaitTime / ThisTime，保留最近 20 条 | `am start -W` |
| **Logcat 实时流** | SSE 推送，按包名 PID 过滤，支持最多 500 行缓冲 | `logcat --pid=` |
| **帧率分析** | 总帧数 / Jank 帧 / 99 分位帧耗时 / Miss Vsync / 高输入延迟 / 慢 UI / 直方图 | `dumpsys gfxinfo` |
| **FPS 实时监控** | framestats 逐帧解析：平均 FPS / Jank 占比 / Vsync 丢失 / 每帧耗时分布柱状图 | `dumpsys gfxinfo framestats` |
| **网络流量排行** | 所有 App 的总流量排名（RX/TX/Total），uid→包名映射 | `dumpsys netstats detail` |
| **内存分解** | 进程级 PSS 分类明细（Native Heap / Dalvik / Graphics / Stack 等 18 类） | `dumpsys meminfo` |
| **Monkey 压测** | 可中断的 Monkey 测试，SSE 实时推送日志，自动统计注入事件数 / 崩溃 / ANR，历史记录 | `monkey -p` |
| **截图对比** | 任意两张截图的像素级差异图（红色高亮差异区域），计算差异像素占比 | `screencap` |

### 四、模式切换

项目支持 **监控模式（Monitor）** 与 **测试模式（Test）** 动态切换：

- **监控模式**：侧重系统级指标，ADB 调用串行化以减少设备负载，App 测试标签页隐藏
- **测试模式**：并行 ThreadPoolExecutor（8 worker）加速数据采集，App 测试标签页全部显示

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        浏览器 (Web Dashboard)                      │
│  http://127.0.0.1:9999                                           │
│  20+ 标签页 / SSE 实时流 / Canvas 图表 / 截图对比                    │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP + SSE
┌───────────────────────────▼──────────────────────────────────────┐
│              Python http.server (BaseHTTPRequestHandler)          │
│  ┌─────────────┐ ┌──────────────┐ ┌─────────────┐                │
│  │ GET /data   │ │ SSE /logcat  │ │ POST /kill  │  ... 30+ 端点  │
│  └──────┬──────┘ └──────┬───────┘ └──────┬──────┘                │
└─────────┼───────────────┼────────────────┼──────────────────────┘
          │               │                │
          ▼               ▼                ▼
┌──────────────────────────────────────────────────────────────────┐
│                      数据采集层 (ADB Shell)                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Monitor Mode  │  │  Test Mode   │  │  常驻后台线程         │   │
│  │ (串行 ADB)    │  │ (ThreadPool) │  │  • 充电采样 (1s)     │   │
│  │              │  │              │  │  • 内存泄漏追踪       │   │
│  │ dumpsys      │  │ dumpsys      │  │  • 电池健康采集       │   │
│  │ /proc/stat   │  │ /proc/meminfo│  │  • 省电策略守护       │   │
│  │ /proc/meminfo│  │ sysfs        │  └──────────────────────┘   │
│  │ sysfs        │  │ ps           │                              │
│  └──────┬───────┘  └──────┬───────┘                              │
└─────────┼─────────────────┼──────────────────────────────────────┘
          │                 │
          ▼                 ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Android 设备 (USB/WiFi ADB)                   │
└──────────────────────────────────────────────────────────────────┘
```

**关键设计决策**：
- 单文件架构（`phone_monitor.py`），零外部依赖，部署即用
- HTML 模板内嵌在 Python 源码中（`HTML_TPL`），无需额外的静态文件服务
- 监控模式与测试模式共用同一份数据采集函数，通过 `MODE` 全局变量路由不同采集策略
- SSE（Server-Sent Events）用于 Logcat 和 Monkey 的实时流推送，前端用 `EventSource` 接收
- 所有持久化数据（电池健康、充电会话、省电策略）均以 JSON 形式存储在项目目录下

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **后端** | Python 3.7+，标准库 `http.server` |
| **前端** | 原生 HTML5 + CSS3 + JavaScript（ES6），无框架 |
| **通信** | RESTful JSON API + SSE（Server-Sent Events） |
| **图表** | Canvas 2D 自绘（温度趋势、充电曲线、帧耗时分布） |
| **设备通信** | Android Debug Bridge (ADB) |
| **并发** | `threading` + `concurrent.futures.ThreadPoolExecutor` + `queue.Queue` |
| **持久化** | JSON 文件（`battery_health.json`、`power_save.json`、充电会话） |

---

## 快速开始

### 环境要求

- **操作系统**：Windows 10 / 11
- **Python**：3.7 及以上（标准库即可，无需 pip 安装任何包）
- **ADB**：Android Platform Tools，默认路径 `C:\Program Files\platform-tools\adb.exe`
- **Android 设备**：已开启 USB 调试并授权

### 安装

```bash
# 1. 克隆仓库
git clone <repo-url> phone_monitor
cd phone_monitor

# 2. 确认 ADB 可用
adb devices
# 应看到设备列表，如：
# List of devices attached
# XXXXXXXX    device
```

### 启动

**方式一：双击运行**

直接双击 `phone_monitor.bat`，浏览器将自动打开仪表盘。

**方式二：命令行**

```bash
python phone_monitor.py
```

启动后浏览器自动打开 `http://127.0.0.1:9999`。

### 停止

在终端按 `Ctrl+C`，或在仪表盘点击「重启服务」按钮。

---

## API 文档

### 核心数据

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/data` | — | JSON 对象 | 完整监控数据（电池/CPU/内存/进程等） |
| GET | `/core` | — | JSON 对象 | 核心指标快照（测试模式用，不含进程列表） |

### 模式切换

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/mode/state` | — | `{"mode": "monitor/test"}` | 获取当前模式 |
| GET | `/mode/switch` | `mode=monitor\|test` | `{"mode": "..."}` | 切换模式 |

### 电池与充电

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/charge/state` | — | `{"active": bool, "points": [...], ...}` | 充电会话实时状态 |
| POST | `/charge/start` | `level=<电量>` | `{"ok": true}` | 创建充电采样会话 |
| GET | `/battery_health` | — | 电池寿命预测结果 | 容量/衰减速率/预计可用天数 |

### 进程管理

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/kill` | `pkg=<包名>` | `{"ok": true, "pkg": "..."}` | 强杀指定进程（系统进程拒绝） |

### 高级诊断

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/wakeup` | — | `[{name, ms, count}, ...]` | 唤醒源统计 TOP20 |
| GET | `/leaks` | — | `[{pkg, start_rss, current_rss, growth_pct, slope}, ...]` | 疑似内存泄漏 TOP5 |
| GET | `/powersave` | — | `{enabled, whitelist, kill_log}` | 省电策略状态 |
| POST | `/powersave/toggle` | — | `{"enabled": bool}` | 开关省电策略 |
| POST | `/powersave/trigger` | — | `{"ok": true}` | 手动触发一次省电策略 |
| POST | `/powersave/whitelist/add` | `pkg=<包名>` | `{"ok": true}` | 添加白名单 |
| POST | `/powersave/whitelist/remove` | `pkg=<包名>` | `{"ok": true}` | 移除白名单 |
| GET | `/cpugov` | — | `{total_cores, cores: [{id, governor, freq_mhz, min_mhz, max_mhz}]}` | CPU 调度详情 |
| GET | `/gpuinfo` | — | `{gpu_model, gpu_freq_mhz, gpu_busy_pct, gpu_available}` | GPU 频率与负载 |
| GET | `/doze` | — | `{idle_mode, light_idle, deep_idle, force_idle, charging, screen_on}` | Doze 状态 |
| GET | `/iostat` | — | `{devices, total_read_mb, total_write_mb}` | IO 增量统计 |
| GET | `/anr` | — | `{files, count, errors}` | ANR 文件检测 |
| GET | `/adb/restart` | — | `{ok, devices, detail}` | 重置 ADB 连接 |

### 性能模式控制

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/perfmode/set` | `mode=performance\|battery\|default&option=<键名>` | `{success, message}` | 设置性能模式 |
| POST | `/perfmode/status` | — | `{power_performance, speed_mode, ...}` | 读取 5 个性能键状态 |

### 系统设置优化

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/sysopt/status` | — | `{wifi_scan_always_enabled: "0/1", ...}` | 读取 13 项设置 |
| POST | `/sysopt/set` | JSON Body: `{setting, value}` | `{success, setting, value, wrote}` | 写入单项设置 |
| POST | `/sysopt/oneshot` | — | `{success, results}` | 一键省电（批量写入） |

### App 测试

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/startup` | `pkg=<包名>` | `{pkg, totalTime, waitTime, thisTime, status}` | 冷启动耗时测试 |
| GET | `/startup/history` | — | `[{pkg, total, wait, thisTime, time}, ...]` | 启动历史（最近 20 条） |
| GET | `/gfxinfo` | `pkg=<包名>` | `{total_frames, janky_frames, percentile_99, histogram, ...}` | 帧率与卡顿分析 |
| GET | `/fps` | `pkg=<包名>` | `{recent_fps, avg_frame_ms, jank_pct, frame_times, ...}` | FPS 实时监控 |
| GET | `/traffic` | — | `[{pkg, rx_kb, tx_kb, total_kb}, ...]` | 网络流量排行 TOP20 |
| GET | `/meminfo` | `pkg=<包名>` | `{pkg, pid, categories,[{name,pss,private_dirty,swapped}], summary}` | 进程内存详细分解 |
| GET | `/monkey/history` | — | `[{pkg, events, crashes, ...}, ...]` | Monkey 历史记录 |

### 截图

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/screenshot/take` | — | `{id, path, time}` | 截图并拉取到本地 |
| GET | `/screenshot/<id>` | — | `image/png` | 获取截图文件 |

### SSE 实时流

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/logcat/stream` | `pkg=<包名>` | SSE 事件流 | Logcat 实时日志推送 |
| POST | `/logcat/stop` | — | `{"ok": true}` | 停止所有 Logcat 流 |
| GET | `/monkey/stream` | `pkg=<包名>&count=<N>&throttle=<ms>` | SSE 事件流 | Monkey 压测实时推送 |
| POST | `/monkey/stop` | — | `{"ok": true}` | 停止所有 Monkey |

### 服务管理

| 方法 | 路径 | 参数 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/restart` | — | `{"ok": true, "message": "..."}` | 重启服务（通过独立 bat 进程） |

---

## 前端页面说明

### 仪表盘（Dashboard）
实时监控首页，包含：
- 9 个核心指标卡片：温度 / SoC 温度 / 电量 / 充放电 / 内存 / CPU 空闲 / 前台应用 / 屏幕 / 当前时间
- 逐核心频率可视化条
- 温度 & 放电趋势折线图（Canvas 2D）
- 活跃唤醒锁标签云
- CPU / 内存 TOP15 进程表格（支持一键强杀）

### 充电曲线
- 充电摘录摘要：起止电量、已充电量、耗时、平均功率、当前温度
- 电量 & 温度双 Canvas 折线图

### 唤醒源
- 唤醒源 TOP20 表格（名称 / 次数 / 总时长）

### 内存泄漏
- 线性回归检测 RSS 持续增长 >15% 超过 15 分钟的进程

### 电池寿命
- 当前容量 / 设计容量 / 衰减率 / 衰减速率 / 预计剩余天数

### 省电策略
- 开关控制 / 白名单管理 / 已杀进程日志

### 冷启动（App 测试）
- 输入包名 → 测试启动 → 显示 TotalTime / WaitTime / ThisTime
- 保留最近 20 条历史记录

### Logcat（App 测试）
- 实时 SSE 日志流，按包名 PID 过滤
- 日志等级着色（V/D/I/W/E/F）
- 最多缓冲 500 行，支持清屏

### 帧率（App 测试）
- gfxinfo 帧统计数据 + 帧耗时分布直方图

### 流量（App 测试）
- 全 App 网络流量 TOP20（接收/发送/总计）

### Monkey（App 测试）
- 可配置事件数 / 间隔，实时推送日志
- 自动统计崩溃 / ANR，保留历史记录

### 截图对比
- 任意时机截图 A / B → 前端 Canvas 像素级差异图（差异区域红色高亮）

### FPS 实时（v6.5）
- framestats 逐帧解析，最近 120 帧的 FPS / Jank 比例 / 帧耗时分布柱状图

### 内存分解（v6.5）
- 进程 18 类内存分类明细（Native Heap / Dalvik / Graphics 等）

### ANR 检测（v6.5）
- /data/anr/ 目录文件列表，traces.txt 异常检测

### IO 性能（v6.5）
- 首次建立基准，再次查询显示增量

### Doze 检测（v6.5）
- 6 项 Doze 状态指标

### CPU 调度（v6.5）
- 性能模式控制区（性能/省电/恢复默认）+ 5 个开关
- CPU 调度状态只读监控表

### 系统设置优化（v6.5）
- 13 项耗电设置可视化开关（WiFi 扫描 / Doze / 定位 / 性能加速 / 同步等）
- 一键省电按钮

### GPU 频率（v6.5）
- GPU 型号 / 频率 / 负载

---

## 配置说明

### ADB 路径

默认 ADB 路径硬编码在 `phone_monitor.py` 第 21 行：

```python
ADB = r"C:\Program Files\platform-tools\adb.exe"
```

如果你的 ADB 安装在其他位置，修改此行即可。

### 端口

默认 Web 服务端口在第 22 行：

```python
PORT = 9999
```

### 刷新间隔

默认数据刷新间隔在第 23 行：

```python
REFRESH_INTERVAL = 2  # 秒
```

### 内存泄漏检测参数

```python
MEM_LEAK_WINDOW = 900  # 15 分钟滑动窗口
```

### 服务器自动重启

仪表盘点击「重启服务」会生成独立 bat 脚本、杀掉当前进程、重新启动。

### 模式持久化

当前模式（monitor/test）保存在 `mode.txt`，重启后保持。

---

## 注意事项

1. **ADB 前置条件**：确保 Android 设备已通过 USB 连接、USB 调试已开启并授权本电脑。使用 `adb devices` 确认设备在线。
2. **端口占用**：服务默认监听 `127.0.0.1:9999`，仅本地可访问。如需局域网访问，修改 `http.server.HTTPServer` 的绑定地址为 `"0.0.0.0"`。
3. **非 Root 设备限制**：部分 sysfs 路径（如 `/sys/class/power_supply/battery/charge_full`）需要 root 权限，程序已内置降级方案（dumpsys batterystats）。
4. **GPU 兼容性**：GPU 信息采集覆盖高通 Adreno、联发科 Mali、MIUI 三条路径，非主流设备可能显示"不可用"。
5. **监控模式 vs 测试模式**：监控模式下 ADB 调用为串行（每条约 5s），适合长期挂机；测试模式下并行采集（ThreadPoolExecutor），适合短时间密集测试。
6. **省电策略风险**：开启后息屏时自动强杀高 CPU 进程，请将重要后台应用加入白名单。
7. **性能模式控制**：依赖 HyperOS/MIUI 的 Settings 键（`POWER_PERFORMANCE_MODE_OPEN` 等），原生 Android 可能无效。
8. **数据存储**：电池健康、充电会话、省电策略均以 JSON 文件存储在项目目录，建议定期归档或清理 `charge_sessions/` 和 `screenshots/`。
9. **index 文件保护**：`mode.txt`、`pid.txt`、`battery_health.json`、`power_save.json` 为运行时文件，请勿手动修改，否则可能导致启动异常。
10. **网络流量统计**：`dumpsys netstats detail` 数据量较大且查询慢（超时 15s），建议非必要时不要频繁调用。
