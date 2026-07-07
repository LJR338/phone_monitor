#!/usr/bin/env python3
"""手机实时监控仪表盘 v5 - 充电曲线+内存泄漏+唤醒源+省电策略+电池寿命"""

import subprocess
import json
import time
import http.server
import threading
import webbrowser
import re
import urllib.parse
import os
import sys
import collections
import math
import tempfile

ADB = r"C:\Program Files\platform-tools\adb.exe"
PORT = 9999
REFRESH_INTERVAL = 2
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# ======================= 全局状态 =======================
prev_stat = {}
prev_stat_lock = threading.Lock()

# 充电曲线
charge_session = {"active": False, "points": [], "start_time": "", "start_level": 0}
charge_lock = threading.Lock()

# 内存泄漏跟踪: pkg -> deque of (timestamp, rss)
mem_history = {}
mem_lock = threading.Lock()
MEM_LEAK_WINDOW = 900  # 约 30 分钟 (2s × 900)

# 电池健康: list of (date, charge_full_uah)
battery_health_data = []
health_lock = threading.Lock()

# 省电策略
power_save_enabled = False
power_save_whitelist = set()
power_save_kill_log = []  # list of (time, pkg)
power_save_lock = threading.Lock()


def adb(cmd):
    try:
        result = subprocess.run([ADB, "shell"] + cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except:
        return ""


# ======================= 数据采集 =======================

def get_charge_full():
    val = adb(["cat", "/sys/class/power_supply/battery/charge_full"])
    try: return int(val) // 1000  # μAh → mAh
    except: return 0


def get_wakeup_sources():
    """解析 dumpsys batterystats 提取唤醒源"""
    raw = adb(["dumpsys", "batterystats"])
    sources = []
    in_wakeup = False
    for line in raw.split("\n"):
        line = line.strip()
        if "Wakeup reason" in line and ":" in line:
            in_wakeup = True
        if in_wakeup and ":" in line and "ms (" in line:
            # 格式: "  abc: 1.5s (3 times) realtime"
            m = re.match(r'\s*(\S+):\s*([\d.]+)([a-z]+)\s*\((\d+)\s*times\)', line)
            if m:
                name = m.group(1)
                val = float(m.group(2))
                unit = m.group(3)
                count = int(m.group(4))
                ms = val * 1000 if unit == "s" else val if unit == "ms" else val * 60 * 1000
                sources.append({"name": name, "ms": round(ms), "count": count})
            else:
                # 简化匹配
                m2 = re.match(r'\s*(\S+):.*?(\d+)\s*times', line)
                if m2:
                    sources.append({"name": m2.group(1), "ms": 0, "count": int(m2.group(2))})
        if in_wakeup and line == "":
            break
    # 按唤醒时长排序
    sources.sort(key=lambda x: x["ms"], reverse=True)
    return sources[:20]


def get_data():
    global prev_stat
    data = {"time": time.strftime("%H:%M:%S"), "timestamp": time.time()}

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

    # 电压
    volt_raw = adb(["cat", "/sys/class/power_supply/battery/voltage_now"])
    try: data["bat_voltage"] = int(volt_raw) / 1000000
    except: data["bat_voltage"] = 0

    # 电流
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
    data["discharge_ma"] = round(curr_ma, 0) if not data.get("charging") and curr_ma > 0 else 0

    chtype = adb(["cat", "/sys/class/power_supply/battery/charge_type"])
    data["charge_type"] = chtype if chtype else "?"

    # ===== SoC 温区 =====
    zones = adb(["cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"])
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
        data["disk_total"] = "?"; data["disk_free"] = "?"; data["disk_pct"] = 0

    # ===== /proc/stat =====
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
                now_stat[core] = {"total": sum(vals), "idle": vals[3] + vals[4]}
    with prev_stat_lock:
        if prev_stat and now_stat:
            for core, nv in now_stat.items():
                pv = prev_stat.get(core)
                if pv and nv["total"] > pv["total"]:
                    dt = nv["total"] - pv["total"]
                    di = nv["idle"] - pv["idle"]
                    idle_pct = round(di / dt * 100, 1) if dt > 0 else 0
                    if core == "cpu": cpu_total_idle_pct = idle_pct
                    else: per_core.append({"core": core, "idle_pct": idle_pct})
            per_core.sort(key=lambda x: int(x["core"].replace("cpu", "")) if x["core"].replace("cpu", "").isdigit() else 999)
        prev_stat = now_stat
    data["cpu_idle_pct"] = cpu_total_idle_pct
    data["per_core"] = per_core

    # ===== CPU 频率 =====
    cpuinfo = adb(["cat", "/proc/cpuinfo"])
    data["cpu_cores"] = cpuinfo.count("processor\t:")
    freq_raw = adb(["cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null"])
    freqs = []
    if freq_raw:
        for f in freq_raw.split("\n"):
            try: freqs.append(int(f.strip()) / 1000)
            except: pass
    data["cpu_freq_avg"] = round(sum(freqs) / len(freqs), 0) if freqs else 0
    data["per_core_freqs"] = freqs

    # ===== 前台应用 =====
    fg_raw = adb(["dumpsys", "activity", "activities"])
    fg_app = ""
    for line in fg_raw.split("\n"):
        if "mResumedActivity" in line or "mFocusedApp" in line:
            parts = line.split()
            for p in parts:
                if "/" in p and "." in p:
                    fg_app = p.split("/")[0]; break
            if fg_app: break
    data["fg_app"] = fg_app[:50] if fg_app else "未知"

    # ===== CPU 进程 TOP =====
    proc_raw = adb(["ps -A -o '%CPU,TCNT,ARGS' --sort=-%cpu"])
    data["top_procs"] = []
    seen_cpu = set()
    for line in proc_raw.split("\n")[1:]:
        if len(data["top_procs"]) >= 15: break
        parts = line.strip().split(None, 2)
        if len(parts) >= 3:
            try:
                name = parts[2][:40]
                pkg = parts[2].split(":")[0].split("/")[0].strip()[:50]
                if pkg in seen_cpu: continue
                seen_cpu.add(pkg)
                cpu = float(parts[0]); tcnt = int(parts[1])
                data["top_procs"].append({"cpu": cpu, "tcnt": tcnt, "name": name, "pkg": pkg})
            except: pass

    # ===== 内存进程 TOP =====
    mem_proc_raw = adb(["ps -A -o '%MEM,RSS,TCNT,ARGS' --sort=-%mem"])
    data["top_mem_procs"] = []
    seen_mem = set()
    for line in mem_proc_raw.split("\n")[1:]:
        if len(data["top_mem_procs"]) >= 15: break
        parts = line.strip().split(None, 3)
        if len(parts) >= 4:
            try:
                name = parts[3][:40]
                pkg = parts[3].split(":")[0].split("/")[0].strip()[:50]
                if pkg in seen_mem: continue
                seen_mem.add(pkg)
                mem_pct = float(parts[0]); rss = round(int(parts[1]) / 1024, 0); tcnt = int(parts[2])
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
            in_section = True; continue
        if in_section and "=" in line:
            m2 = re.match(r'\s*Wake Lock (\S+)', line)
            if m2: data["wakelocks"].append(m2.group(1)[:40])
            if len(data["wakelocks"]) >= 5: break

    return data


# ======================= 充电曲线管理 =======================

def charge_sample_thread():
    """充电时每秒采样，不阻塞主循环"""
    global charge_session
    while True:
        with charge_lock:
            active = charge_session["active"]
        if active:
            try:
                bat = adb(["dumpsys", "battery"])
                lvl = temp = volt = curr = 0
                for line in bat.split("\n"):
                    line = line.strip()
                    if "level:" in line:
                        try: lvl = int(line.split(":")[1].strip())
                        except: pass
                    elif "temperature:" in line:
                        try: temp = int(line.split(":")[1].strip()) / 10
                        except: pass
                volt_raw = adb(["cat", "/sys/class/power_supply/battery/voltage_now"])
                try: volt = int(volt_raw) / 1000000
                except: pass
                curr_raw = adb(["cat", "/sys/class/power_supply/battery/current_now"])
                try: curr = abs(int(curr_raw)) / 1000
                except: pass

                point = {
                    "ts": time.time(),
                    "time": time.strftime("%H:%M:%S"),
                    "level": lvl, "temp": round(temp, 1),
                    "voltage": round(volt, 3),
                    "current": round(curr, 0),
                    "power": round(curr * volt / 1000, 1) if volt > 0 and curr > 0 else 0
                }
                with charge_lock:
                    charge_session["points"].append(point)
                    # 充满或断开时结束
                    if lvl >= 100:
                        _save_charge_session()
            except Exception:
                pass
            time.sleep(1)
        else:
            time.sleep(2)


def start_charge_session(level):
    global charge_session
    with charge_lock:
        charge_session["active"] = True
        charge_session["points"] = []
        charge_session["start_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        charge_session["start_level"] = level


def _save_charge_session():
    """结束当前充电会话并保存到文件"""
    global charge_session
    now = time.strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(DATA_DIR, "charge_sessions", f"session_{now}.json")
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    session = {
        "start_time": charge_session["start_time"],
        "end_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "start_level": charge_session["start_level"],
        "end_level": charge_session["points"][-1]["level"] if charge_session["points"] else 0,
        "points": charge_session["points"]
    }
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False)
    charge_session["active"] = False
    charge_session["points"] = []


def get_charge_state():
    with charge_lock:
        return {
            "active": charge_session["active"],
            "start_time": charge_session["start_time"],
            "start_level": charge_session["start_level"],
            "point_count": len(charge_session["points"]),
            "points": list(charge_session["points"])
        }


# ======================= 内存泄漏检测 =======================

def update_mem_history(top_mem_procs):
    t = time.time()
    with mem_lock:
        for p in top_mem_procs:
            pkg = p["pkg"]
            if pkg not in mem_history:
                mem_history[pkg] = collections.deque(maxlen=MEM_LEAK_WINDOW)
            mem_history[pkg].append((t, p["rss"]))


def check_mem_leaks():
    """检查内存泄漏：RSS 持续单向增长超过 30 分钟"""
    leaks = []
    with mem_lock:
        for pkg, samples in list(mem_history.items()):
            if len(samples) < 450:  # ~15 分钟数据
                continue
            recent = list(samples)[-450:]
            rss_vals = [s[1] for s in recent]
            if len(rss_vals) < 100: continue
            # 检查趋势：线性回归斜率 > 0 且增长 > 15%
            n = len(rss_vals)
            x_mean = (n - 1) / 2
            y_mean = sum(rss_vals) / n
            num = sum((i - x_mean) * (rss_vals[i] - y_mean) for i in range(n))
            den = sum((i - x_mean) ** 2 for i in range(n))
            slope = num / den if den > 0 else 0
            growth_pct = (rss_vals[-1] - rss_vals[0]) / max(rss_vals[0], 1) * 100
            if slope > 0.001 and growth_pct > 15:
                leaks.append({
                    "pkg": pkg, "start_rss": round(rss_vals[0]),
                    "current_rss": round(rss_vals[-1]),
                    "growth_pct": round(growth_pct, 1),
                    "slope": round(slope, 3)
                })
    leaks.sort(key=lambda x: x["growth_pct"], reverse=True)
    return leaks[:5]


# ======================= 电池寿命预测 =======================

def update_battery_health(force=False):
    """每小时记录一次 charge_full"""
    global battery_health_data
    t = time.time()
    should_update = force or len(battery_health_data) == 0
    if len(battery_health_data) > 0:
        last_ts = battery_health_data[-1].get("ts", 0) if isinstance(battery_health_data[-1], dict) else 0
        if t - last_ts > 3600:
            should_update = True
    if not should_update: return

    cf = get_charge_full()
    if cf > 0:
        hfile = os.path.join(DATA_DIR, "battery_health.json")
        with health_lock:
            entry = {"ts": t, "date": time.strftime("%Y-%m-%d %H:%M"), "charge_full": cf}
            battery_health_data.append(entry)
            # 保留最近 200 条
            if len(battery_health_data) > 200:
                battery_health_data = battery_health_data[-200:]
            try:
                with open(hfile, "w", encoding="utf-8") as f:
                    json.dump(battery_health_data, f, ensure_ascii=False)
            except: pass


def predict_battery_life():
    """线性回归预测电池寿命"""
    with health_lock:
        data = list(battery_health_data)
    if len(data) < 3:
        return {"status": "need_more_data", "message": "需要至少 3 条记录才能预测", "records": len(data)}

    n = len(data)
    vals = [d["charge_full"] for d in data]
    x_mean = (n - 1) / 2
    y_mean = sum(vals) / n
    num = sum((i - x_mean) * (vals[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0
    intercept = y_mean - slope * x_mean
    current_val = intercept + slope * (n - 1)

    # 找到首次记录的设计容量近似值
    design_cap = max(vals)
    # 预测到达 80% 的天数
    threshold = design_cap * 0.8
    if slope >= 0:
        return {"status": "stable", "current": round(current_val), "design": design_cap,
                "degradation": round((1 - current_val / design_cap) * 100, 1),
                "message": "容量稳定，未检测到衰减趋势"}
    days_to_80 = (threshold - current_val) / slope / 24 if slope < 0 else -1
    if days_to_80 < 0: days_to_80 = 9999

    return {
        "status": "degrading",
        "current": round(current_val),
        "design": design_cap,
        "degradation": round((1 - current_val / design_cap) * 100, 1),
        "slope_per_day": round(slope * 24, 2),  # mAh/天
        "days_to_80pct": round(days_to_80, 0),
        "records": n
    }


# ======================= 省电策略 =======================

def handle_screen_off_power_save(screen_on, top_procs):
    """检测到息屏时执行省电策略"""
    global power_save_enabled, power_save_kill_log
    if not power_save_enabled or screen_on:
        return

    with power_save_lock:
        killed = []
        for p in top_procs:
            pkg = p["pkg"]
            if pkg in power_save_whitelist: continue
            if pkg.startswith("com.android") or pkg.startswith("system"): continue
            if p["cpu"] > 10:
                try:
                    subprocess.run([ADB, "shell", "am", "force-stop", pkg],
                                   capture_output=True, timeout=5)
                    killed.append(pkg)
                except: pass
        for pkg in killed:
            power_save_kill_log.append({"time": time.strftime("%H:%M:%S"), "pkg": pkg})
            if len(power_save_kill_log) > 50:
                power_save_kill_log = power_save_kill_log[-50:]


# ======================= 初始化加载 =======================

def load_persisted_data():
    global battery_health_data, power_save_enabled, power_save_whitelist
    # 电池健康
    hfile = os.path.join(DATA_DIR, "battery_health.json")
    if os.path.exists(hfile):
        try:
            with open(hfile, "r", encoding="utf-8") as f:
                battery_health_data = json.load(f)
        except: pass
    # 省电策略状态
    psfile = os.path.join(DATA_DIR, "power_save.json")
    if os.path.exists(psfile):
        try:
            with open(psfile, "r", encoding="utf-8") as f:
                ps = json.load(f)
                power_save_enabled = ps.get("enabled", False)
                power_save_whitelist = set(ps.get("whitelist", []))
        except: pass
    # 开机时记录一次电池健康
    cf = get_charge_full()
    if cf > 0:
        update_battery_health(force=True)


def save_power_save_state():
    psfile = os.path.join(DATA_DIR, "power_save.json")
    with power_save_lock:
        state = {"enabled": power_save_enabled, "whitelist": sorted(list(power_save_whitelist))}
    try:
        with open(psfile, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except: pass


# ======================= HTML 模板 =======================

HTML_TPL = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>手机实时监控 v5</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Consolas','Microsoft YaHei',monospace; padding: 16px; }
h1 { font-size: 15px; margin-bottom: 6px; color: #58a6ff; display: flex; align-items: center; gap: 10px; }
.btn { font-size: 9px; padding: 3px 10px; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; border-radius: 4px; cursor: pointer; }
.btn:hover { background: #30363d; }
.btn-on { background: #2ea043; border-color: #2ea043; }
.btn-off { background: #F44336; border-color: #F44336; }
.tabs { display: flex; gap: 2px; margin-bottom: 10px; border-bottom: 1px solid #30363d; }
.tab { font-size: 9px; padding: 5px 12px; background: transparent; color: #8b949e; border: none; cursor: pointer; border-bottom: 2px solid transparent; }
.tab:hover { color: #c9d1d9; }
.tab.active { color: #58a6ff; border-bottom-color: #58a6ff; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; margin-bottom: 8px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 7px 9px; }
.card-title { font-size: 8px; color: #8b949e; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.5px; }
.big-num { font-size: 22px; font-weight: bold; }
.unit { font-size: 10px; color: #8b949e; }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
.col { flex: 1; min-width: 320px; }
.table-card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; overflow: hidden; margin-bottom: 6px; }
.table-card th { background: #21262d; font-size: 8px; color: #8b949e; text-align: left; padding: 4px 7px; text-transform: uppercase; }
.table-card td { padding: 3px 7px; font-size: 10px; border-top: 1px solid #21262d; }
.bar-bg { background: #21262d; height: 2px; border-radius: 1px; margin-top: 2px; }
.bar-fill { height: 2px; border-radius: 1px; transition: width 1s; }
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
.leak-row { color: #FF9800; }
.leak-row.severe { color: #F44336; }
.footer { text-align: center; font-size: 8px; color: #484f58; margin-top: 10px; }
input[type="text"] { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 6px; font-size: 9px; border-radius: 3px; width: 160px; font-family: inherit; }
.add-btn { font-size: 9px; padding: 2px 8px; background: #2ea043; border: none; color: #fff; border-radius: 3px; cursor: pointer; }
.remove-btn { font-size: 8px; color: #F44336; cursor: pointer; margin-left: 6px; }
.whitelist-item { display: inline-block; font-size: 8px; background: #21262d; padding: 2px 6px; border-radius: 3px; margin: 2px; }
</style>
</head>
<body>
<h1>手机实时监控 v5
<span style="flex:1"></span>
<button class="btn" onclick="exportCSV()">CSV导出</button>
<button class="btn" onclick="restartServer()" title="重启服务以加载最新代码">重启服务</button>
</h1>
<div class="tabs">
<button class="tab active" onclick="switchTab('dashboard')">仪表盘</button>
<button class="tab" onclick="switchTab('charge')">充电曲线</button>
<button class="tab" onclick="switchTab('wakeup')">唤醒源</button>
<button class="tab" onclick="switchTab('leaks')">内存泄漏</button>
<button class="tab" onclick="switchTab('battery')">电池寿命</button>
<button class="tab" onclick="switchTab('powersave')">省电策略</button>
</div>

<div id="tab-dashboard" class="tab-content active">
<div id="dash"></div>
</div>

<div id="tab-charge" class="tab-content">
<div id="charge"></div>
</div>

<div id="tab-wakeup" class="tab-content">
<div id="wakeup"></div>
</div>

<div id="tab-leaks" class="tab-content">
<div id="leaks"></div>
</div>

<div id="tab-battery" class="tab-content">
<div id="battery-health"></div>
</div>

<div id="tab-powersave" class="tab-content">
<div id="powersave"></div>
</div>

<div class="toast" id="toast"></div>
<div class="footer">Marvis Phone Monitor v5</div>

<script>
const MAX_HISTORY = 30;
const ALERT_THRESHOLD = 20;
const ALERT_SAMPLES = 5;
let history = [];
let alertTracker = {};
let currentTab = 'dashboard';
let chargePoints = [];
let prevCharging = false;
let prevScreenOn = true;

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
    t.textContent = msg; t.style.background = bg || '#F44336'; t.style.color = '#fff';
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2000);
}

function switchTab(name) {
    currentTab = name;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
    if (name === 'charge') renderChargeChart();
    if (name === 'leaks') loadLeaks();
    if (name === 'wakeup') loadWakeup();
    if (name === 'battery') loadBatteryHealth();
    if (name === 'powersave') loadPowerSave();
}

function killProcess(pkg, el) {
    if (!confirm('强杀 ' + pkg + ' ?')) return;
    el.style.opacity = '0.3'; el.style.pointerEvents = 'none';
    fetch('/kill?pkg=' + encodeURIComponent(pkg)).then(r => r.json()).then(r => {
        showToast(r.ok ? '已强杀 ' + pkg : '失败', r.ok ? '#4CAF50' : '#F44336');
    }).catch(() => {});
}

function checkAlerts(topProcs) {
    let seen = {};
    (topProcs || []).forEach(p => { if (p.cpu >= ALERT_THRESHOLD) seen[p.pkg || extractPkg(p.name)] = p; });
    Object.keys(alertTracker).forEach(k => { if (!seen[k]) alertTracker[k] = 0; });
    Object.keys(seen).forEach(k => { alertTracker[k] = (alertTracker[k] || 0) + 1; });
    Object.entries(alertTracker).forEach(([k, v]) => {
        if (v >= ALERT_SAMPLES) {
            alertTracker[k] = -999;
            let p = seen[k] || {};
            if (Notification.permission === 'granted') {
                new Notification('CPU 异常', { body: (p.name||k).substring(0,30) + ' ' + (p.cpu||0).toFixed(1) + '%' });
            }
            showToast('⚠ ' + (p.name||k) + ' CPU ' + (p.cpu||0).toFixed(1) + '%', '#F44336');
        }
    });
}

function drawChart(history, canvasId, key, color, label, maxVal) {
    let c = document.getElementById(canvasId);
    if (!c) return;
    let ctx = c.getContext('2d');
    let W = c.parentElement.clientWidth;
    let H = 90;
    c.width = W * 2; c.height = H * 2; c.style.width = W + 'px'; c.style.height = H + 'px';
    ctx.scale(2, 2);
    ctx.fillStyle = '#161b22'; ctx.fillRect(0, 0, W, H);
    if (history.length < 2) return;
    let vals = history.map(d => d[key] || 0);
    let mx = maxVal || Math.max(1, ...vals);
    ctx.strokeStyle = '#21262d'; ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        let y = 8 + (H - 16) * i / 4;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
        ctx.fillStyle = '#484f58'; ctx.font = '7px Consolas';
        ctx.fillText((mx * (1 - i/4)).toFixed(0), 2, y - 2);
    }
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    let step = W / Math.max(history.length - 1, 1);
    for (let i = 0; i < history.length; i++) {
        let x = i * step;
        let y = 8 + (H - 16) * (1 - vals[i] / mx);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = color; ctx.font = 'bold 8px Consolas';
    ctx.fillText(label + ' ' + vals[vals.length-1].toFixed(1), W - 90, 12);
}

function renderDash(d) {
    const batPct = d.bat_level || 0;
    const batClr = batPct > 50 ? "#4CAF50" : batPct > 20 ? "#FF9800" : "#F44336";
    const chgClr = d.charging ? "#4CAF50" : "#484f58";
    const chgArrow = d.charging ? "↑" : (d.charge_status == "放电中" ? "↓" : "");

    let wlTags = "";
    (d.wakelocks || []).forEach(w => { wlTags += '<span class="wl-tag">' + w + '</span>'; });
    if (!wlTags) wlTags = '<span style="color:#484f58;font-size:8px">无</span>';

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
        let tag = p.cpu > 10 ? '<span class="tag" style="background:#F44336;color:#fff">高</span>' : '';
        let pkg = p.pkg || extractPkg(p.name);
        let killBtn = pkg ? '<span class="kill-btn" onclick="killProcess(\'' + pkg + '\',this)">X</span>' : '';
        let thrClr = (p.tcnt||0) > 100 ? '#F44336' : (p.tcnt||0) > 50 ? '#FF9800' : '#8b949e';
        procRows += "<tr><td>" + (i+1) + "</td><td><span class='process-name' title='" + p.name + "'>" + p.name + "</span>" + tag + killBtn + "</td><td style='color:" + thrClr + ";text-align:center'>" + (p.tcnt||0) + "</td><td style='color:" + clr + ";font-weight:bold'>" + p.cpu.toFixed(1) + "%</td><td>" + bar + "</td></tr>";
    });

    let memProcRows = "";
    (d.top_mem_procs || []).forEach((p, i) => {
        const clr = p.mem_pct < 2 ? "#4CAF50" : p.mem_pct < 5 ? "#FF9800" : "#F44336";
        let pkg = p.pkg || extractPkg(p.name);
        let killBtn = pkg ? '<span class="kill-btn" onclick="killProcess(\'' + pkg + '\',this)">X</span>' : '';
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
    let sleepClr = (d.cpu_idle_pct||0) > 90 ? "#4CAF50" : (d.cpu_idle_pct||0) > 70 ? "#FF9800" : "#F44336";
    html += '<div class="card"><div class="card-title">CPU 空闲</div><div class="num-sm" style="color:' + sleepClr + '">' + (d.cpu_idle_pct||0).toFixed(0) + '<span class="unit">%</span></div></div>';
    html += '<div class="card"><div class="card-title">前台</div><div class="num-sm" style="font-size:10px;color:#c9d1d9">' + (d.fg_app||"") + '</div></div>';
    html += '<div class="card"><div class="card-title">屏幕</div><div style="font-size:10px;">' + (d.screen_on ? '<span style="color:#58a6ff">亮屏</span>' : '<span style="color:#484f58">息屏</span>') + '  |  ' + (d.fps||"?") + '</div></div>';
    html += '<div class="card"><div class="card-title">时间</div><div class="num-sm" style="color:#58a6ff">' + d.time + '</div></div>';
    html += '</div>';
    html += '<div class="card" style="margin-bottom:8px"><div class="card-title">各核心频率</div><div style="line-height:18px">' + coreBars + '</div></div>';
    html += '<div class="chart-card"><div class="chart-title">历史趋势</div>';
    html += '<div class="row"><div class="col"><canvas id="chart_temp"></canvas></div><div class="col"><canvas id="chart_discharge"></canvas></div></div>';
    html += '<div class="row"><div class="col"><canvas id="chart_mem"></canvas></div><div class="col"><canvas id="chart_cpu"></canvas></div></div></div>';
    html += '<div class="card" style="margin-bottom:6px"><div class="card-title">唤醒锁</div><div>' + wlTags + '</div></div>';
    html += '<div class="row">';
    html += '<div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>线程</th><th>CPU</th><th>负载</th></tr>' + procRows + '</table></div></div>';
    html += '<div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>线程</th><th>内存</th><th>RSS</th></tr>' + memProcRows + '</table></div></div>';
    html += '</div>';
    document.getElementById("dash").innerHTML = html;
}

function renderChargeChart() {
    let html = '<div class="card" style="margin-bottom:8px"><div class="card-title">充电曲线 - 当前会话</div>';
    if (chargePoints.length === 0) {
        html += '<div style="color:#484f58;font-size:10px;padding:20px">暂无充电数据，插上充电器后自动开始记录</div>';
    } else {
        html += '<div style="font-size:9px;color:#8b949e;margin-bottom:4px">' + chargePoints.length + ' 个采样点</div>';
        html += '<canvas id="chart_charge_curve" style="height:250px"></canvas>';
    }
    html += '</div>';
    document.getElementById("charge").innerHTML = html;
    if (chargePoints.length > 0) drawChargeCurve();
}

function drawChargeCurve() {
    let c = document.getElementById("chart_charge_curve");
    if (!c || chargePoints.length < 2) return;
    let ctx = c.getContext('2d');
    let W = c.parentElement.clientWidth;
    let H = 250;
    c.width = W * 2; c.height = H * 2; c.style.width = W + 'px'; c.style.height = H + 'px';
    ctx.scale(2, 2);
    ctx.fillStyle = '#161b22'; ctx.fillRect(0, 0, W, H);

    // 左侧 Y 轴：温度 (°C) , 右侧 Y 轴：电流 (mA)
    let margin = { top: 20, right: 50, bottom: 30, left: 40 };
    let pw = W - margin.left - margin.right;
    let ph = H - margin.top - margin.bottom;

    // 温度范围 20-50
    let tempMin = 20, tempMax = 50;
    // 电流范围 0-max
    let maxCurr = Math.max(1000, ...chargePoints.map(p => p.current || 0));
    // 电量范围
    let minLvl = Math.min(...chargePoints.map(p => p.level || 100));
    let maxLvl = Math.max(...chargePoints.map(p => p.level || 0));

    let step = pw / Math.max(chargePoints.length - 1, 1);
    let stepX = function(i) { return margin.left + i * step; };

    // 网格
    ctx.strokeStyle = '#21262d'; ctx.lineWidth = 0.5;
    for (let g = 0; g <= 4; g++) {
        let y = margin.top + ph * g / 4;
        ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(W - margin.right, y); ctx.stroke();
    }

    // 温度曲线 (橙色)
    ctx.strokeStyle = '#FF9800'; ctx.lineWidth = 2; ctx.beginPath();
    for (let i = 0; i < chargePoints.length; i++) {
        let x = stepX(i);
        let pct = (chargePoints[i].temp - tempMin) / (tempMax - tempMin);
        let y = margin.top + ph * (1 - Math.max(0, Math.min(1, pct)));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#FF9800'; ctx.font = 'bold 9px Consolas';
    ctx.fillText('温度 ' + chargePoints[chargePoints.length-1].temp + '°C', W - margin.right - 80, margin.top - 5);

    // 电流曲线 (绿色)
    ctx.strokeStyle = '#4CAF50'; ctx.lineWidth = 2; ctx.beginPath();
    for (let i = 0; i < chargePoints.length; i++) {
        let x = stepX(i);
        let pct = (chargePoints[i].current || 0) / maxCurr;
        let y = margin.top + ph * (1 - Math.min(1, pct));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#4CAF50';
    ctx.fillText('电流 ' + (chargePoints[chargePoints.length-1].current||0) + 'mA', W - margin.right - 80, margin.top + 10);

    // 电量曲线 (蓝色，用右侧辅助 Y)
    ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]); ctx.beginPath();
    for (let i = 0; i < chargePoints.length; i++) {
        let x = stepX(i);
        let pct = (chargePoints[i].level - minLvl) / Math.max(maxLvl - minLvl, 1);
        let y = margin.top + ph * (1 - pct);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#58a6ff';
    ctx.fillText('电量 ' + chargePoints[chargePoints.length-1].level + '%', W - margin.right - 80, margin.top + 25);

    // Y 轴标签
    ctx.fillStyle = '#8b949e'; ctx.font = '7px Consolas';
    ctx.fillText(tempMin + '°C', 2, margin.top + ph);
    ctx.fillText(tempMax + '°C', 2, margin.top + 8);
    ctx.fillText(maxCurr + 'mA', W - margin.right + 4, margin.top + 8);
    ctx.fillText('0mA', W - margin.right + 4, margin.top + ph);
}

// ========== 唤醒源 ==========
function loadWakeup() {
    fetch('/wakeup').then(r => r.json()).then(data => {
        let rows = "";
        data.forEach((s, i) => {
            let ms = s.ms || 0;
            let t = ms >= 60000 ? (ms/60000).toFixed(1)+'min' : ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms';
            rows += '<tr><td>' + (i+1) + '</td><td>' + s.name + '</td><td>' + s.count + '</td><td>' + t + '</td></tr>';
        });
        if (!rows) rows = '<tr><td colspan="4" style="color:#484f58">未找到唤醒源数据</td></tr>';
        document.getElementById('wakeup').innerHTML =
            '<div class="table-card"><table width="100%"><tr><th>#</th><th>唤醒源</th><th>次数</th><th>总时长</th></tr>' + rows + '</table></div>';
    });
}

// ========== 内存泄漏 ==========
function loadLeaks() {
    fetch('/leaks').then(r => r.json()).then(data => {
        let rows = "";
        data.forEach((l, i) => {
            let cls = l.growth_pct > 50 ? 'severe' : '';
            rows += '<tr class="leak-row ' + cls + '"><td>' + (i+1) + '</td><td>' + l.pkg + '</td><td>' + l.start_rss + ' MB</td><td>' + l.current_rss + ' MB</td><td>' + l.growth_pct + '%</td><td>' + l.slope + ' MB/s</td></tr>';
        });
        if (!rows) rows = '<tr><td colspan="6" style="color:#4CAF50">未检测到疑似内存泄漏</td></tr>';
        document.getElementById('leaks').innerHTML =
            '<div class="card" style="margin-bottom:8px"><div class="card-title">内存泄漏检测 - 持续 RSS 增长 >15% 超过 15 分钟</div></div>' +
            '<div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>起始 RSS</th><th>当前 RSS</th><th>增长</th><th>速率</th></tr>' + rows + '</table></div>';
    });
}

// ========== 电池寿命 ==========
function loadBatteryHealth() {
    fetch('/battery_health').then(r => r.json()).then(data => {
        if (data.status === 'need_more_data') {
            document.getElementById('battery-health').innerHTML =
                '<div class="card"><div class="card-title">电池寿命预测</div><div style="padding:20px;color:#8b949e;font-size:10px">' + data.message + '（当前 ' + data.records + ' 条）</div></div>';
            return;
        }
        let daysClr = data.days_to_80pct < 90 ? '#F44336' : data.days_to_80pct < 365 ? '#FF9800' : '#4CAF50';
        let html = '<div class="grid">';
        html += '<div class="card"><div class="card-title">当前容量</div><div class="big-num" style="color:#58a6ff">' + data.current + '<span class="unit">mAh</span></div></div>';
        html += '<div class="card"><div class="card-title">设计容量</div><div class="big-num" style="color:#8b949e">' + data.design + '<span class="unit">mAh</span></div></div>';
        html += '<div class="card"><div class="card-title">已衰减</div><div class="big-num" style="color:' + (data.degradation > 20 ? '#F44336' : '#FF9800') + '">' + data.degradation + '<span class="unit">%</span></div></div>';
        html += '<div class="card"><div class="card-title">衰减速率</div><div class="num-sm" style="color:#FF9800">' + data.slope_per_day + '<span class="unit">mAh/天</span></div></div>';
        html += '<div class="card"><div class="card-title">预计可用</div><div class="big-num" style="color:' + daysClr + '">' + (data.days_to_80pct < 9998 ? data.days_to_80pct : '>10000') + '<span class="unit">天</span></div><div class="label">到达 80% 容量</div></div>';
        html += '<div class="card"><div class="card-title">记录数</div><div class="num-sm" style="color:#8b949e">' + (data.records||0) + '<span class="unit">条</span></div></div>';
        html += '</div>';
        document.getElementById('battery-health').innerHTML = html;
    });
}

// ========== 省电策略 ==========
function loadPowerSave() {
    fetch('/powersave').then(r => r.json()).then(data => {
        let toggleBtn = data.enabled
            ? '<button class="btn btn-on" onclick="togglePowerSave()">已开启 - 点击关闭</button>'
            : '<button class="btn btn-off" onclick="togglePowerSave()">已关闭 - 点击开启</button>';
        let whItems = "";
        (data.whitelist || []).forEach(w => {
            whItems += '<span class="whitelist-item">' + w + '<span class="remove-btn" onclick="removeWhitelist(\'' + w + '\')">✕</span></span>';
        });
        if (!whItems) whItems = '<span style="color:#484f58;font-size:9px">无</span>';

        let killRows = "";
        (data.kill_log || []).slice().reverse().forEach(k => {
            killRows += '<tr><td>' + k.time + '</td><td>' + k.pkg + '</td></tr>';
        });
        if (!killRows) killRows = '<tr><td colspan="2" style="color:#484f58">无</td></tr>';

        let html = '<div class="card" style="margin-bottom:8px"><div class="card-title">息屏省电策略</div>';
        html += '<div style="margin:8px 0">' + toggleBtn + '</div>';
        html += '<div style="font-size:8px;color:#8b949e;margin-top:6px">息屏时自动强杀 CPU>10% 的非白名单进程</div></div>';
        html += '<div class="card" style="margin-bottom:8px"><div class="card-title">白名单（不会被杀）</div>';
        html += '<div style="margin:6px 0">' + whItems + '</div>';
        html += '<input type="text" id="whInput" placeholder="输入包名，如 com.tencent.mm"><button class="add-btn" onclick="addWhitelist()">添加</button></div>';
        html += '<div class="table-card"><table width="100%"><tr><th>时间</th><th>已杀进程</th></tr>' + killRows + '</table></div>';
        document.getElementById('powersave').innerHTML = html;
    });
}

function togglePowerSave() {
    fetch('/powersave/toggle', {method:'POST'}).then(r => r.json()).then(() => loadPowerSave());
}

function addWhitelist() {
    let v = document.getElementById('whInput').value.trim();
    if (!v) return;
    fetch('/powersave/whitelist/add?pkg=' + encodeURIComponent(v), {method:'POST'}).then(r => r.json()).then(() => { loadPowerSave(); });
}

function removeWhitelist(pkg) {
    fetch('/powersave/whitelist/remove?pkg=' + encodeURIComponent(pkg), {method:'POST'}).then(r => r.json()).then(() => { loadPowerSave(); });
}

// ========== 服务重启 ==========
function restartServer() {
    if (!confirm('确定要重启监控服务吗？')) return;
    fetch('/restart', {method:'POST'}).then(r => r.json()).then(d => {
        showToast(d.message || '重启中...', '#58a6ff');
        // 等 3 秒后自动刷新页面
        setTimeout(() => { location.reload(); }, 3000);
    }).catch(() => {
        showToast('重启失败，请手动重启', '#F44336');
    });
}

// ========== CSV 导出 ==========
function exportCSV() {
    if (history.length === 0) return;
    let keys = ['time','temp','soc_max','bat_level','bat_voltage','charge_current','charge_power','discharge_ma',
                'mem_used_pct','swap_pct','disk_pct','cpu_freq_avg','cpu_idle_pct','screen_on','fg_app'];
    let csv = '\uFEFF' + keys.join(',') + '\n';
    history.forEach(d => {
        csv += keys.map(k => { let v = d[k]; if (typeof v === 'boolean') return v?'1':'0'; return v != null ? v : ''; }).join(',') + '\n';
    });
    let blob = new Blob([csv], {type:'text/csv;charset=utf-8'});
    let a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'monitor_' + new Date().toISOString().slice(0,19).replace(/:/g,'-') + '.csv';
    a.click(); URL.revokeObjectURL(a.href);
    showToast('CSV 已导出', '#4CAF50');
}

// ========== 主循环 ==========
let failCount = 0;
function load() {
    fetch("/data").then(r => r.json()).then(d => {
        failCount = 0;
        history.push(d);
        if (history.length > MAX_HISTORY) history.shift();
        checkAlerts(d.top_procs);

        if (currentTab === 'dashboard') renderDash(d);

        // 充电状态切换检测
        if (d.charging && !prevCharging) {
            fetch('/charge/start?level=' + d.bat_level, {method:'POST'}).catch(()=>{});
        }
        prevCharging = d.charging;

        // 充电曲线实时渲染
        if (currentTab === 'charge') {
            fetch('/charge/state').then(r => r.json()).then(cs => {
                chargePoints = cs.points || [];
                renderChargeChart();
            }).catch(()=>{});
        }

        // 息屏省电检测
        if (!d.screen_on && prevScreenOn) {
            fetch('/powersave/trigger', {method:'POST'}).catch(()=>{});
        }
        prevScreenOn = d.screen_on;

        // 图表只在仪表盘渲染
        if (currentTab === 'dashboard') {
            if (history.length > 0) {
                drawChart(history, 'chart_temp', 'temp', '#FF9800', '温度', 50);
                drawChart(history, 'chart_discharge', 'discharge_ma', '#FF9800', '放电mA', Math.max(500, ...history.map(d => d.discharge_ma||0)));
                drawChart(history, 'chart_mem', 'mem_used_pct', '#FF9800', '内存%', 100);
            }
            if (history.length > 0) {
                let cpuHistory = history.map(d => (d.top_procs||[]).reduce((s,p)=>s+(p.cpu||0), 0));
                drawChart(history.map((d,i)=>Object.assign({},d,{_tc:cpuHistory[i]})), 'chart_cpu', '_tc', '#4CAF50', '进程CPU', Math.max(20,...cpuHistory));
            }
        }
    }).catch(() => {
        failCount++;
        if (failCount >= 3) {
            document.getElementById("dash").innerHTML = "ADB 连接失败，请检查手机连接";
        }
    });
}

load();
setInterval(load, __INTERVAL__);
</script>
</body>
</html>"""

HTML = HTML_TPL.replace("__REFRESH__", str(REFRESH_INTERVAL)).replace("__INTERVAL__", str(REFRESH_INTERVAL * 1000))


# ======================= 服务重启 =======================

SCRIPT_PATH = os.path.abspath(__file__)
CURRENT_PID = os.getpid()

def do_restart():
    """生成临时批处理脚本，异步杀掉旧进程并启动新进程"""
    bat = f'''@echo off
timeout /t 2 /nobreak >nul
taskkill /f /pid {CURRENT_PID} >nul 2>&1
start "" python "{SCRIPT_PATH}"
del "%~f0"
'''
    fd, path = tempfile.mkstemp(suffix=".bat", prefix="monitor_restart_")
    with os.fdopen(fd, "w") as f:
        f.write(bat)
    subprocess.Popen(["cmd", "/c", path], creationflags=subprocess.CREATE_NEW_CONSOLE | 0x00000008)


# ======================= HTTP Handler =======================

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _wrap(self, method):
        """包装请求处理，抑制连接中断异常"""
        try:
            method()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self):
        self._wrap(self._do_GET)

    def _do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/data":
            data = get_data()
            # 顺便更新内存历史
            update_mem_history(data.get("top_mem_procs", []))
            # 检查电池健康（每小时一次）
            update_battery_health()
            self._json(data)

        elif parsed.path == "/kill":
            pkg = qs.get("pkg", [""])[0]
            if pkg and not pkg.startswith("com.android") and not pkg.startswith("system"):
                try:
                    subprocess.run([ADB, "shell", "am", "force-stop", pkg], capture_output=True, timeout=5)
                    self._json({"ok": True, "pkg": pkg})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
            else:
                self._json({"ok": False, "error": "不允许强杀系统进程"})

        elif parsed.path == "/charge/state":
            self._json(get_charge_state())

        elif parsed.path == "/wakeup":
            self._json(get_wakeup_sources())

        elif parsed.path == "/leaks":
            self._json(check_mem_leaks())

        elif parsed.path == "/battery_health":
            self._json(predict_battery_life())

        elif parsed.path == "/powersave":
            with power_save_lock:
                self._json({
                    "enabled": power_save_enabled,
                    "whitelist": sorted(list(power_save_whitelist)),
                    "kill_log": list(power_save_kill_log)
                })

        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def do_POST(self):
        self._wrap(self._do_POST)

    def _do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/charge/start":
            level = int(qs.get("level", ["0"])[0])
            start_charge_session(level)
            self._json({"ok": True})

        elif parsed.path == "/powersave/toggle":
            global power_save_enabled
            with power_save_lock:
                power_save_enabled = not power_save_enabled
            save_power_save_state()
            self._json({"enabled": power_save_enabled})

        elif parsed.path == "/powersave/trigger":
            # 由前端在检测到息屏时触发
            data = get_data()
            if not data.get("screen_on"):
                handle_screen_off_power_save(False, data.get("top_procs", []))
            self._json({"ok": True})

        elif parsed.path == "/powersave/whitelist/add":
            pkg = qs.get("pkg", [""])[0]
            if pkg:
                with power_save_lock:
                    power_save_whitelist.add(pkg)
                save_power_save_state()
            self._json({"ok": True})

        elif parsed.path == "/powersave/whitelist/remove":
            pkg = qs.get("pkg", [""])[0]
            with power_save_lock:
                power_save_whitelist.discard(pkg)
            save_power_save_state()
            self._json({"ok": True})

        elif parsed.path == "/restart":
            self._json({"ok": True, "message": "正在重启服务..."})
            threading.Thread(target=do_restart, daemon=True).start()

        else:
            self._json({"ok": False, "error": "unknown endpoint"})


# ======================= Main =======================

def main():
    print("启动手机监控仪表盘 v5 ...")
    load_persisted_data()

    # 充电采样线程
    ct = threading.Thread(target=charge_sample_thread, daemon=True)
    ct.start()

    # 端口绑定重试（旧进程可能还没释放端口）
    server = None
    for attempt in range(10):
        try:
            server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            print(f"端口 {PORT} 被占用，重试 {attempt+1}/10...")
            time.sleep(1)
    if server is None:
        print(f"无法绑定端口 {PORT}，请手动关闭占用进程后重试")
        return

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
