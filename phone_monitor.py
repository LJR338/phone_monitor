#!/usr/bin/env python3
"""手机实时功耗监控仪表盘 v4 - 线程数+放电曲线+核心频率+深度睡眠+异常告警+CSV导出"""

import subprocess
import json
import time
import http.server
import threading
import webbrowser
import re
import urllib.parse

ADB = r"C:\Program Files\platform-tools\adb.exe"
PORT = 9999
REFRESH_INTERVAL = 2

# 用于计算 /proc/stat 增量（核心负载 + 深度睡眠时间）
prev_stat = {}
prev_stat_lock = threading.Lock()

def adb(cmd):
    try:
        result = subprocess.run([ADB, "shell"] + cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except:
        return ""

def get_data():
    global prev_stat
    data = {"time": time.strftime("%H:%M:%S")}

    # ===== 电池 =====
    bat = adb(["dumpsys", "battery"])
    for line in bat.split("\n"):
        line = line.strip()
        if "temperature:" in line:
            try: data["temp"] = int(line.split(":")[1].strip()) / 10
            except: data["temp"] = 0
        elif "level:" in line:
            try: data["bat_level"] = int(line.split(":")[1].strip())
            except: data["bat_level"] = 0
        elif "health:" in line:
            try: data["bat_health"] = int(line.split(":")[1].strip())
            except: data["bat_health"] = 1
        elif "status:" in line:
            try:
                s = int(line.split(":")[1].strip())
                data["charge_status"] = {2:"充电中",3:"放电中",4:"未充电",5:"已充满"}.get(s, "未知")
                data["charging"] = s == 2
            except: data["charge_status"] = "?"
        elif "voltage:" in line:
            try: data["bat_voltage"] = int(line.split(":")[1].strip()) / 1000
            except: data["bat_voltage"] = 0

    curr = adb(["cat", "/sys/class/power_supply/battery/current_now"])
    try:
        curr_val = int(curr)
        curr_ma = abs(curr_val) / 1000 if curr else 0
    except:
        curr_ma = 0
        curr_val = 0

    data["charge_current"] = round(curr_ma, 0)
    if data.get("bat_voltage", 0) > 0 and curr_ma > 0:
        data["charge_power"] = round(curr_ma * data["bat_voltage"] / 1000, 1)
    else:
        data["charge_power"] = 0

    # 放电速率（仅放电时有效）
    data["discharge_ma"] = round(curr_ma, 0) if not data.get("charging") and curr_ma > 0 else 0

    chtype = adb(["cat", "/sys/class/power_supply/battery/charge_type"])
    data["charge_type"] = chtype if chtype else "?"

    # ===== SoC 温区 =====
    zones = adb(["cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null".replace("'", '"')])
    temps = []
    if zones:
        for t in zones.split("\n"):
            try: temps.append(int(t.strip()) / 1000)
            except: pass
    data["soc_max"] = round(max(temps), 1) if temps else 0

    # ===== 内存 =====
    mem_raw = adb(["cat", "/proc/meminfo"])
    mem = {}
    for line in mem_raw.split("\n"):
        parts = line.split(":")
        if len(parts) == 2:
            key = parts[0].strip()
            try: mem[key] = int(parts[1].strip().split()[0])
            except: pass
    data["mem_total"] = round(mem.get("MemTotal", 0) / 1048576, 1)
    data["mem_avail"] = round(mem.get("MemAvailable", 0) / 1048576, 1)
    data["mem_used_pct"] = round((1 - mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1)) * 100, 1)
    swap_total = mem.get("SwapTotal", 0) / 1048576
    swap_free = mem.get("SwapFree", 0) / 1048576
    data["swap_pct"] = round((1 - swap_free / max(swap_total, 1)) * 100, 1) if swap_total > 0 else 0

    # ===== 存储 =====
    df_raw = adb(["df", "-h", "/data"])
    m = re.search(r'/data\s+(\d+\.?\d*[MG])\s+\d+\.?\d*[MG]\s+(\d+\.?\d*[MG])\s+(\d+)%', df_raw) if df_raw else None
    if m:
        data["disk_total"] = m.group(1)
        data["disk_free"] = m.group(2)
        data["disk_pct"] = int(m.group(3))
    else:
        data["disk_total"] = "?"
        data["disk_free"] = "?"
        data["disk_pct"] = 0

    # =====  /proc/stat - 核心负载 + 深度睡眠 =====
    stat_raw = adb(["cat", "/proc/stat"])
    cpu_total_idle_pct = 0
    per_core = []
    now_stat = {}
    if stat_raw:
        for line in stat_raw.split("\n"):
            fields = line.split()
            if len(fields) >= 5 and fields[0].startswith("cpu"):
                core = fields[0]
                vals = [int(x) for x in fields[1:8]]
                total = sum(vals)
                idle = vals[3] + vals[4]  # idle + iowait
                now_stat[core] = {"total": total, "idle": idle}

    with prev_stat_lock:
        if prev_stat and now_stat:
            for core, nv in now_stat.items():
                pv = prev_stat.get(core)
                if pv and nv["total"] > pv["total"]:
                    dt = nv["total"] - pv["total"]
                    di = nv["idle"] - pv["idle"]
                    idle_pct = round(di / dt * 100, 1) if dt > 0 else 0
                    if core == "cpu":
                        cpu_total_idle_pct = idle_pct
                    else:
                        per_core.append({"core": core, "idle_pct": idle_pct})
            # 按核心编号排序
            per_core.sort(key=lambda x: int(x["core"].replace("cpu", "")) if x["core"].replace("cpu", "").isdigit() else 999)
        prev_stat = now_stat

    data["cpu_idle_pct"] = cpu_total_idle_pct
    data["deep_sleep_pct"] = cpu_total_idle_pct  # 深睡就用总体空闲率近似
    data["per_core"] = per_core

    # ===== CPU 频率 =====
    cpuinfo = adb(["cat", "/proc/cpuinfo"])
    data["cpu_cores"] = cpuinfo.count("processor\t:")
    freq_raw = adb(["cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null".replace("'", '"')])
    freqs = []
    if freq_raw:
        for f in freq_raw.split("\n"):
            try: freqs.append(int(f.strip()) / 1000)
            except: pass
    data["cpu_freq_avg"] = round(sum(freqs) / len(freqs), 0) if freqs else 0
    data["cpu_freq_min"] = round(min(freqs), 0) if freqs else 0
    data["per_core_freqs"] = freqs  # 所有核心频率列表

    # ===== 前台应用 =====
    fg_raw = adb(["dumpsys", "activity", "activities"])
    fg_app = ""
    for line in fg_raw.split("\n"):
        if "mResumedActivity" in line or "mFocusedApp" in line:
            parts = line.split()
            for p in parts:
                if "/" in p and "." in p:
                    fg_app = p.split("/")[0]
                    break
            if fg_app: break
    data["fg_app"] = fg_app[:50] if fg_app else "未知"

    # ===== CPU 进程 TOP (带线程数) =====
    proc_raw = adb(["ps -A -o '%CPU,TCNT,ARGS' --sort=-%cpu".replace("'", '"')])
    data["top_procs"] = []
    seen_cpu = set()
    for line in proc_raw.split("\n")[1:]:
        if len(data["top_procs"]) >= 15:
            break
        parts = line.strip().split(None, 2)
        if len(parts) >= 3:
            try:
                name = parts[2][:40]
                pkg = parts[2].split(":")[0].split("/")[0].strip()[:50]
                if pkg in seen_cpu:
                    continue
                seen_cpu.add(pkg)
                cpu = float(parts[0])
                tcnt = int(parts[1])
                data["top_procs"].append({"cpu": cpu, "tcnt": tcnt, "name": name, "pkg": pkg})
            except: pass

    # ===== 内存进程 TOP (带线程数) =====
    mem_proc_raw = adb(["ps -A -o '%MEM,RSS,TCNT,ARGS' --sort=-%mem".replace("'", '"')])
    data["top_mem_procs"] = []
    seen_mem = set()
    for line in mem_proc_raw.split("\n")[1:]:
        if len(data["top_mem_procs"]) >= 15:
            break
        parts = line.strip().split(None, 3)
        if len(parts) >= 4:
            try:
                name = parts[3][:40]
                pkg = parts[3].split(":")[0].split("/")[0].strip()[:50]
                if pkg in seen_mem:
                    continue
                seen_mem.add(pkg)
                mem_pct = float(parts[0])
                rss = round(int(parts[1]) / 1024, 0)
                tcnt = int(parts[2])
                data["top_mem_procs"].append({"mem_pct": mem_pct, "rss": rss, "tcnt": tcnt, "name": name, "pkg": pkg})
            except: pass

    # ===== 屏幕 =====
    wm_raw = adb(["dumpsys", "window", "policy"])
    data["screen_on"] = "SCREEN_STATE_ON" in wm_raw
    display_raw = adb(["dumpsys", "display"])
    for line in display_raw.split("\n"):
        if "fps=" in line and "activeModeId" not in line:
            for part in line.split(","):
                if "fps=" in part:
                    try: data["fps"] = part.split("=")[1].strip()
                    except: pass

    # ===== 唤醒锁 =====
    wl_raw = adb(["dumpsys", "power"])
    data["wakelocks"] = []
    in_section = False
    for line in wl_raw.split("\n"):
        if "Wake Locks: size=" in line:
            in_section = True
            continue
        if in_section and "=" in line:
            m2 = re.match(r'\s*Wake Lock (\S+)', line)
            if m2:
                data["wakelocks"].append(m2.group(1)[:40])
            if len(data["wakelocks"]) >= 5:
                break

    return data


HTML_TPL = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>手机实时监控 v4</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Consolas', 'Microsoft YaHei', monospace; padding: 16px; }
h1 { font-size: 15px; margin-bottom: 8px; color: #58a6ff; display: flex; align-items: center; gap: 10px; }
.btn { font-size: 9px; padding: 3px 10px; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; border-radius: 4px; cursor: pointer; }
.btn:hover { background: #30363d; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; margin-bottom: 8px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 7px 9px; }
.card-title { font-size: 8px; color: #8b949e; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.5px; }
.big-num { font-size: 22px; font-weight: bold; }
.unit { font-size: 10px; color: #8b949e; }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.col { flex: 1; min-width: 320px; }
.col-sm { flex: 0.5; min-width: 200px; }
.table-card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; overflow: hidden; margin-bottom: 6px; }
.table-card th { background: #21262d; font-size: 8px; color: #8b949e; text-align: left; padding: 4px 7px; text-transform: uppercase; }
.table-card td { padding: 3px 7px; font-size: 10px; border-top: 1px solid #21262d; }
.bar-bg { background: #21262d; height: 2px; border-radius: 1px; margin-top: 2px; }
.bar-fill { height: 2px; border-radius: 1px; transition: width 1s; }
.footer { text-align: center; font-size: 8px; color: #484f58; margin-top: 6px; }
.process-name { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; }
.tag { font-size: 7px; padding: 1px 3px; border-radius: 2px; margin-left: 2px; }
.num-sm { font-size: 13px; font-weight: bold; }
.label { font-size: 8px; color: #8b949e; }
.wl-tag { display: inline-block; font-size: 8px; background: #21262d; padding: 1px 5px; border-radius: 3px; margin: 1px; max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kill-btn { display: inline-block; width: 13px; height: 13px; line-height: 12px; text-align: center; background: #F44336; color: #fff; border-radius: 2px; cursor: pointer; font-size: 8px; margin-left: 2px; vertical-align: middle; opacity: 0.5; transition: opacity 0.15s; user-select: none; }
.kill-btn:hover { opacity: 1; }
.toast { position: fixed; top: 10px; right: 10px; padding: 7px 14px; border-radius: 5px; font-size: 10px; z-index: 999; display: none; max-width: 320px; }
.core-bar { display: inline-block; width: 60px; height: 10px; background: #21262d; border-radius: 2px; vertical-align: middle; margin: 1px 2px; position: relative; }
.core-bar-fill { position: absolute; left: 0; top: 0; height: 100%; border-radius: 2px; }
.core-label { display: inline-block; width: 24px; font-size: 8px; color: #8b949e; text-align: right; margin-right: 2px; }
.chart-card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 6px; margin-bottom: 8px; }
.chart-title { font-size: 8px; color: #8b949e; margin-bottom: 3px; text-transform: uppercase; }
canvas { display: block; width: 100%; }
td.thr { color: #8b949e; font-size: 9px; text-align: center; min-width: 28px; }
</style>
</head>
<body>
<h1>手机实时监控 v4 - 每 __REFRESH__ 秒刷新
<button class="btn" onclick="exportCSV()">CSV 导出</button>
</h1>
<div id="app">加载中...</div>
<div class="toast" id="toast"></div>
<div class="footer">Marvis Phone Monitor v4</div>
<script>
const MAX_HISTORY = 30;
const ALERT_THRESHOLD = 20;
const ALERT_SAMPLES = 5;
let history = [];
let alertTracker = {};

if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
}

function pc(pct) { return pct < 50 ? "#4CAF50" : pct < 75 ? "#FF9800" : "#F44336"; }
function tc(t) { return t < 35 ? "#4CAF50" : t < 40 ? "#FF9800" : "#F44336"; }
function cc(cpu) { return cpu < 5 ? "#4CAF50" : cpu < 15 ? "#FF9800" : "#F44336"; }

function extractPkg(name) {
    let n = (name||"").trim();
    let m = n.match(/^([a-z][a-z0-9_.]*[a-z0-9])/);
    return m ? m[1] : "";
}

function showToast(msg, bg) {
    let t = document.getElementById('toast');
    t.textContent = msg;
    t.style.background = bg || '#F44336';
    t.style.color = '#fff';
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2000);
}

function killProcess(pkg, el) {
    if (!confirm('强杀 ' + pkg + ' ?')) return;
    el.style.opacity = '0.3';
    el.style.pointerEvents = 'none';
    fetch('/kill?pkg=' + encodeURIComponent(pkg))
        .then(r => r.json())
        .then(r => {
            showToast(r.ok ? '已强杀 ' + pkg : '失败: ' + (r.error||''), r.ok ? '#4CAF50' : '#F44336');
        })
        .catch(() => {});
}

function checkAlerts(topProcs) {
    let seen = {};
    (topProcs || []).forEach(p => {
        if (p.cpu >= ALERT_THRESHOLD) {
            let key = p.pkg || extractPkg(p.name);
            seen[key] = p;
        }
    });

    Object.keys(alertTracker).forEach(k => {
        if (!seen[k]) alertTracker[k] = 0;
    });

    Object.keys(seen).forEach(k => {
        alertTracker[k] = (alertTracker[k] || 0) + 1;
    });

    Object.entries(alertTracker).forEach(([k, v]) => {
        if (v >= ALERT_SAMPLES) {
            alertTracker[k] = -999; // 防止重复告警
            let p = seen[k] || {};
            let cpu = (p.cpu || 0).toFixed(1);
            let name = (p.name || k).substring(0, 30);
            if (Notification.permission === 'granted') {
                new Notification('CPU 异常告警', { body: name + ' ' + cpu + '% 持续高负载', icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="15" fill="%23F44336"/><text x="50" y="68" text-anchor="middle" fill="white" font-size="50" font-weight="bold">!</text></svg>' });
            }
            showToast('⚠ ' + name + ' CPU ' + cpu + '% 持续高负载！', '#F44336');
        }
    });
}

function exportCSV() {
    if (history.length === 0) return;
    let keys = ['time', 'temp', 'soc_max', 'bat_level', 'bat_voltage', 'charge_current', 'charge_power', 'discharge_ma',
                'mem_used_pct', 'swap_pct', 'disk_pct', 'cpu_freq_avg', 'cpu_idle_pct', 'screen_on', 'fg_app'];
    let header = keys.join(',');
    let rows = history.map(d => keys.map(k => {
        let v = d[k];
        if (typeof v === 'boolean') return v ? '1' : '0';
        if (typeof v === 'string' && v.includes(',')) return '"' + v + '"';
        return v != null ? v : '';
    }).join(','));
    let csv = header + '\n' + rows.join('\n');
    let blob = new Blob(['\uFEFF' + csv], {type: 'text/csv;charset=utf-8'});
    let url = URL.createObjectURL(blob);
    let a = document.createElement('a');
    a.href = url;
    a.download = 'phone_monitor_' + new Date().toISOString().slice(0,19).replace(/:/g,'-') + '.csv';
    a.click();
    URL.revokeObjectURL(url);
    showToast('CSV 已导出', '#4CAF50');
}

let failCount = 0;
function load() {
    fetch("/data")
        .then(r => r.json())
        .then(d => {
            failCount = 0;
            history.push(d);
            if (history.length > MAX_HISTORY) history.shift();
            checkAlerts(d.top_procs);
            render(d);
        })
        .catch(() => {
            failCount++;
            if (failCount >= 3) {
                document.getElementById("app").innerHTML = "ADB 连接失败，请检查手机连接";
            }
        });
}

function drawChart(history, canvasId, key, color, label, maxVal) {
    let c = document.getElementById(canvasId);
    if (!c) return;
    let ctx = c.getContext('2d');
    let W = c.parentElement.clientWidth;
    let H = 90;
    c.width = W * 2;
    c.height = H * 2;
    c.style.width = W + 'px';
    c.style.height = H + 'px';
    ctx.scale(2, 2);

    ctx.fillStyle = '#161b22';
    ctx.fillRect(0, 0, W, H);

    if (history.length < 2) return;

    let vals = history.map(d => d[key] || 0);
    let mx = maxVal || Math.max(1, ...vals);

    ctx.strokeStyle = '#21262d';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        let y = 8 + (H - 16) * i / 4;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
        ctx.fillStyle = '#484f58'; ctx.font = '7px Consolas';
        ctx.fillText((mx * (1 - i/4)).toFixed(0), 2, y - 2);
    }

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let step = W / Math.max(history.length - 1, 1);
    for (let i = 0; i < history.length; i++) {
        let x = i * step;
        let y = 8 + (H - 16) * (1 - vals[i] / mx);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.font = 'bold 8px Consolas';
    ctx.fillText(label + ' ' + vals[vals.length-1].toFixed(1), W - 90, 12);
}

function render(d) {
    const batPct = d.bat_level || 0;
    const batClr = batPct > 50 ? "#4CAF50" : batPct > 20 ? "#FF9800" : "#F44336";
    const healthMap = {1:"良好",2:"过热",3:"已坏",4:"过压",5:"未知"};
    const chgClr = d.charging ? "#4CAF50" : "#484f58";
    const chgArrow = d.charging ? "↑" : (d.charge_status == "放电中" ? "↓" : "");

    let wlTags = "";
    (d.wakelocks || []).forEach(w => { wlTags += '<span class="wl-tag">' + w + '</span>'; });
    if (!wlTags) wlTags = '<span style="color:#484f58;font-size:8px">无</span>';

    // 核心频率条
    let coreBars = "";
    (d.per_core_freqs || []).forEach((f, i) => {
        let pct = Math.min(f / 3000 * 100, 100);
        let clr = f > 2000 ? "#F44336" : f > 1000 ? "#FF9800" : "#4CAF50";
        coreBars += '<span class="core-label">C' + i + '</span><span class="core-bar"><span class="core-bar-fill" style="width:' + pct + '%;background:' + clr + '"></span></span>';
    });
    if (!coreBars) coreBars = '<span style="color:#484f58;font-size:9px">-</span>';

    let procRows = "";
    (d.top_procs || []).forEach((p, i) => {
        const clr = cc(p.cpu);
        const bar = p.cpu > 2 ? '<div class="bar-bg"><div class="bar-fill" style="width:' + Math.min(p.cpu, 100) + '%;background:' + clr + '"></div></div>' : "";
        let tag = p.cpu > 10 ? '<span class="tag" style="background:#F44336;color:#fff">高</span>' : p.cpu > 5 ? '<span class="tag" style="background:#FF9800;color:#000">中</span>' : '';
        let pkg = p.pkg || extractPkg(p.name);
        let killBtn = pkg ? '<span class="kill-btn" onclick="killProcess(\'' + pkg + '\',this)" title="强杀 ' + pkg + '">X</span>' : '';
        let thrClr = (p.tcnt||0) > 100 ? '#F44336' : (p.tcnt||0) > 50 ? '#FF9800' : '#8b949e';
        procRows += "<tr><td>" + (i+1) + "</td><td><span class='process-name' title='" + p.name + "'>" + p.name + "</span>" + tag + killBtn + "</td><td style='color:" + thrClr + ";text-align:center'>" + (p.tcnt||0) + "</td><td style='color:" + clr + ";font-weight:bold'>" + p.cpu.toFixed(1) + "%</td><td>" + bar + "</td></tr>";
    });

    let memProcRows = "";
    (d.top_mem_procs || []).forEach((p, i) => {
        const clr = p.mem_pct < 2 ? "#4CAF50" : p.mem_pct < 5 ? "#FF9800" : "#F44336";
        let pkg = p.pkg || extractPkg(p.name);
        let killBtn = pkg ? '<span class="kill-btn" onclick="killProcess(\'' + pkg + '\',this)" title="强杀 ' + pkg + '">X</span>' : '';
        let thrClr = (p.tcnt||0) > 100 ? '#F44336' : (p.tcnt||0) > 50 ? '#FF9800' : '#8b949e';
        memProcRows += "<tr><td>" + (i+1) + "</td><td><span class='process-name' title='" + p.name + "'>" + p.name + "</span>" + killBtn + "</td><td style='color:" + thrClr + ";text-align:center'>" + (p.tcnt||0) + "</td><td style='color:" + clr + ";font-weight:bold'>" + p.mem_pct.toFixed(1) + "%</td><td>" + p.rss + " MB</td></tr>";
    });

    let html = "";
    html += '<div class="grid">';
    html += '<div class="card"><div class="card-title">温度</div><div class="big-num" style="color:' + tc(d.temp) + '">' + (d.temp||0).toFixed(1) + '<span class="unit">°C</span></div></div>';
    html += '<div class="card"><div class="card-title">SoC 最高</div><div class="big-num" style="color:' + tc(d.soc_max) + '">' + (d.soc_max||0).toFixed(1) + '<span class="unit">°C</span></div></div>';
    html += '<div class="card"><div class="card-title">电量</div><div class="big-num" style="color:' + batClr + '">' + batPct + '<span class="unit">%</span></div><div class="label">' + (d.bat_voltage||0).toFixed(1) + 'V</div></div>';
    html += '<div class="card"><div class="card-title">充/放电</div><div class="num-sm" style="color:' + chgClr + '">' + chgArrow + ' ' + (d.charge_current||0) + '<span class="unit">mA</span></div><div class="label">' + (d.charge_power||0) + 'W  |  ' + (d.charge_type||"") + '</div></div>';
    html += '<div class="card"><div class="card-title">放电</div><div class="num-sm" style="color:#FF9800">' + (d.discharge_ma||0) + '<span class="unit">mA</span></div></div>';
    html += '<div class="card"><div class="card-title">内存</div><div class="big-num" style="color:' + pc(d.mem_used_pct) + '">' + (d.mem_used_pct||0).toFixed(1) + '<span class="unit">%</span></div><div class="label">' + (d.mem_avail||0).toFixed(1) + 'GB 可用</div></div>';
    html += '<div class="card"><div class="card-title">交换</div><div class="num-sm" style="color:' + pc(d.swap_pct) + '">' + (d.swap_pct||0).toFixed(0) + '<span class="unit">%</span></div></div>';
    html += '<div class="card"><div class="card-title">存储</div><div class="num-sm" style="color:' + pc(d.disk_pct) + '">' + (d.disk_pct||0) + '<span class="unit">%</span></div><div class="label">' + (d.disk_free||"?") + ' 剩</div></div>';
    html += '<div class="card"><div class="card-title">CPU 平均</div><div class="num-sm" style="color:#58a6ff">' + (d.cpu_freq_avg||0) + '<span class="unit">MHz</span></div><div class="label">' + (d.cpu_cores||"?") + '核</div></div>';
    let sleepClr = (d.deep_sleep_pct||0) > 90 ? "#4CAF50" : (d.deep_sleep_pct||0) > 70 ? "#FF9800" : "#F44336";
    html += '<div class="card"><div class="card-title">CPU 空闲</div><div class="num-sm" style="color:' + sleepClr + '">' + (d.deep_sleep_pct||0).toFixed(0) + '<span class="unit">%</span></div></div>';
    html += '<div class="card"><div class="card-title">前台</div><div class="num-sm" style="font-size:10px;color:#c9d1d9">' + (d.fg_app||"") + '</div></div>';
    html += '<div class="card"><div class="card-title">屏幕</div><div style="font-size:10px;">' + (d.screen_on ? '<span style="color:#58a6ff">亮屏</span>' : '<span style="color:#484f58">息屏</span>') + '  |  ' + (d.fps||"?") + '</div></div>';
    html += '<div class="card"><div class="card-title">时间</div><div class="num-sm" style="color:#58a6ff">' + d.time + '</div></div>';
    html += '</div>';

    // 核心频率条
    html += '<div class="card" style="margin-bottom:8px"><div class="card-title">各核心频率 (MHz / 3GHz量程)</div><div style="line-height:18px">' + coreBars + '</div></div>';

    // 历史曲线 2x2
    html += '<div class="chart-card"><div class="chart-title">历史趋势</div>';
    html += '<div class="row">';
    html += '<div class="col"><canvas id="chart_temp"></canvas></div>';
    html += '<div class="col"><canvas id="chart_discharge"></canvas></div>';
    html += '</div><div class="row">';
    html += '<div class="col"><canvas id="chart_mem"></canvas></div>';
    html += '<div class="col"><canvas id="chart_cpu"></canvas></div>';
    html += '</div></div>';

    html += '<div class="card" style="margin-bottom:6px"><div class="card-title">唤醒锁</div><div>' + wlTags + '</div></div>';

    html += '<div class="row">';
    html += '<div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>线程</th><th>CPU</th><th>负载</th></tr>' + procRows + '</table></div></div>';
    html += '<div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>线程</th><th>内存</th><th>RSS</th></tr>' + memProcRows + '</table></div></div>';
    html += '</div>';

    document.getElementById("app").innerHTML = html;

    if (history.length > 0) {
        drawChart(history, 'chart_temp', 'temp', '#FF9800', '温度', 50);
        drawChart(history, 'chart_discharge', 'discharge_ma', '#FF9800', '放电mA', Math.max(500, ...history.map(d => d.discharge_ma||0)));
        drawChart(history, 'chart_mem', 'mem_used_pct', '#FF9800', '内存%', 100);
    }
    if (history.length > 0) {
        let cpuHistory = history.map(d => {
            let procs = d.top_procs || [];
            return procs.reduce((sum, p) => sum + (p.cpu||0), 0);
        });
        let tmpData = history.map((d, i) => Object.assign({}, d, {_total_cpu: cpuHistory[i]}));
        drawChart(tmpData, 'chart_cpu', '_total_cpu', '#4CAF50', '进程CPU', Math.max(20, ...cpuHistory));
    }
}
load();
setInterval(load, __INTERVAL__);
</script>
</body>
</html>"""

HTML = HTML_TPL.replace("__REFRESH__", str(REFRESH_INTERVAL)).replace("__INTERVAL__", str(REFRESH_INTERVAL * 1000))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/data":
            data = get_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif parsed.path == "/kill":
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = qs.get("pkg", [""])[0]
            if pkg and not pkg.startswith("com.android") and not pkg.startswith("system"):
                try:
                    subprocess.run([ADB, "shell", "am", "force-stop", pkg],
                                   capture_output=True, timeout=5)
                    result = {"ok": True, "pkg": pkg}
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            else:
                result = {"ok": False, "error": "不允许强杀系统进程"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())


def main():
    print("启动手机监控仪表盘 v4 ...")
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    print(f"仪表盘已打开: http://127.0.0.1:{PORT}")
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
