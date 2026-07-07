#!/usr/bin/env python3
"""手机实时监控仪表盘 v6 - v5 + App测试(冷启动/logcat/帧率/流量/Monkey/截图对比)"""

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
from concurrent.futures import ThreadPoolExecutor
import tempfile
import queue
import uuid

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

# 内存泄漏跟踪
mem_history = {}
mem_lock = threading.Lock()
MEM_LEAK_WINDOW = 900

# 电池健康
battery_health_data = []
health_lock = threading.Lock()

# 省电策略
power_save_enabled = False
power_save_whitelist = set()
power_save_kill_log = []
power_save_lock = threading.Lock()

# v6 - Logcat 流
logcat_streams = {}  # stream_id -> {"proc": Popen, "queue": Queue, "stop": Event, "pkg": str}
logcat_lock = threading.Lock()

# v6 - Monkey 流
monkey_streams = {}
monkey_lock = threading.Lock()

# v6 - 截图缓冲
screenshot_store = {}  # shot_id -> filepath
screenshot_lock = threading.Lock()

# v6 - 冷启动历史
startup_history = collections.deque(maxlen=20)  # list of {pkg, total, wait, thisTime, time}
startup_lock = threading.Lock()

# v6 - Monkey 结果历史
monkey_results = collections.deque(maxlen=10)
monkey_results_lock = threading.Lock()


def adb(cmd):
    try:
        result = subprocess.run([ADB, "shell"] + cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except:
        return ""


# ======================= v5 数据采集 (保持不变) =======================

def get_charge_full():
    val = adb(["cat", "/sys/class/power_supply/battery/charge_full"])
    try: return int(val) // 1000
    except: return 0


def get_wakeup_sources():
    raw = adb(["dumpsys", "batterystats"])
    sources = []
    in_wakeup = False
    for line in raw.split("\n"):
        line = line.strip()
        if "Wakeup reason" in line and ":" in line:
            in_wakeup = True
        if in_wakeup and ":" in line and "ms (" in line:
            m = re.match(r'\s*(\S+):\s*([\d.]+)([a-z]+)\s*\((\d+)\s*times\)', line)
            if m:
                name, val, unit, count = m.group(1), float(m.group(2)), m.group(3), int(m.group(4))
                ms = val * 1000 if unit == "s" else val if unit == "ms" else val * 60 * 1000
                sources.append({"name": name, "ms": round(ms), "count": count})
            else:
                m2 = re.match(r'\s*(\S+):.*?(\d+)\s*times', line)
                if m2: sources.append({"name": m2.group(1), "ms": 0, "count": int(m2.group(2))})
        if in_wakeup and line == "": break
    sources.sort(key=lambda x: x["ms"], reverse=True)
    return sources[:20]


def get_data():
    global prev_stat
    data = {"time": time.strftime("%H:%M:%S"), "timestamp": time.time()}

    with ThreadPoolExecutor(max_workers=8) as ex:
        f_bat = ex.submit(adb, ["dumpsys", "battery"])
        f_batt_sys = ex.submit(adb, ["cat", "/sys/class/power_supply/battery/voltage_now",
            "/sys/class/power_supply/battery/current_now", "/sys/class/power_supply/battery/charge_type"])
        f_thermal = ex.submit(adb, ["cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"])
        f_meminfo = ex.submit(adb, ["cat", "/proc/meminfo"])
        f_df = ex.submit(adb, ["df", "-h", "/data"])
        f_stat = ex.submit(adb, ["cat", "/proc/stat"])
        f_cpuinfo = ex.submit(adb, ["cat", "/proc/cpuinfo"])
        f_freq = ex.submit(adb, ["cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null"])
        f_fg = ex.submit(adb, ["dumpsys", "activity", "activities"])
        f_ps = ex.submit(adb, ["ps", "-A", "-o", "%CPU,%MEM,RSS,TCNT,ARGS"])
        f_wm = ex.submit(adb, ["dumpsys", "window", "policy"])
        f_display = ex.submit(adb, ["dumpsys", "display"])
        f_power = ex.submit(adb, ["dumpsys", "power"])

        bat = f_bat.result(); batt_sys = f_batt_sys.result(); zones = f_thermal.result()
        mem_raw = f_meminfo.result(); df_raw = f_df.result(); stat_raw = f_stat.result()
        cpuinfo = f_cpuinfo.result(); freq_raw = f_freq.result(); fg_raw = f_fg.result()
        proc_raw = f_ps.result(); wm_raw = f_wm.result(); display_raw = f_display.result()
        wl_raw = f_power.result()

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

    bs_lines = batt_sys.strip().split("\n")
    if len(bs_lines) >= 1:
        try: data["bat_voltage"] = int(bs_lines[0].strip()) / 1000000
        except: data["bat_voltage"] = 0
    if len(bs_lines) >= 2:
        try: curr_val = int(bs_lines[1].strip()); curr_ma = abs(curr_val) / 1000 if bs_lines[1].strip() else 0
        except: curr_ma = 0
    else: curr_ma = 0
    data["charge_current"] = round(curr_ma, 0)
    data["charge_power"] = round(curr_ma * data.get("bat_voltage", 0) / 1000, 1) if data.get("bat_voltage", 0) > 0 and curr_ma > 0 else 0
    data["discharge_ma"] = round(curr_ma, 0) if not data.get("charging") and curr_ma > 0 else 0
    data["charge_type"] = bs_lines[2].strip() if len(bs_lines) >= 3 and bs_lines[2].strip() else "?"

    temps = []
    if zones:
        for t in zones.split("\n"):
            try: temps.append(int(t.strip()) / 1000)
            except: pass
    data["soc_max"] = round(max(temps), 1) if temps else 0

    mem = {}
    for line in mem_raw.split("\n"):
        parts = line.split(":")
        if len(parts) == 2:
            try: mem[parts[0].strip()] = int(parts[1].strip().split()[0])
            except: pass
    data["mem_total"] = round(mem.get("MemTotal", 0) / 1048576, 1)
    data["mem_avail"] = round(mem.get("MemAvailable", 0) / 1048576, 1)
    data["mem_used_pct"] = round((1 - mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1)) * 100, 1)
    swap_total = mem.get("SwapTotal", 0) / 1048576
    swap_free = mem.get("SwapFree", 0) / 1048576
    data["swap_pct"] = round((1 - swap_free / max(swap_total, 1)) * 100, 1) if swap_total > 0 else 0

    m = re.search(r'/data\s+(\d+\.?\d*[MG])\s+\d+\.?\d*[MG]\s+(\d+\.?\d*[MG])\s+(\d+)%', df_raw) if df_raw else None
    if m: data["disk_total"], data["disk_free"], data["disk_pct"] = m.group(1), m.group(2), int(m.group(3))
    else: data["disk_total"], data["disk_free"], data["disk_pct"] = "?", "?", 0

    cpu_total_idle_pct = 0; per_core = []; now_stat = {}
    if stat_raw:
        for line in stat_raw.split("\n"):
            fields = line.split()
            if len(fields) >= 5 and fields[0].startswith("cpu"):
                vals = [int(x) for x in fields[1:8]]
                now_stat[fields[0]] = {"total": sum(vals), "idle": vals[3] + vals[4]}
    with prev_stat_lock:
        if prev_stat and now_stat:
            for core, nv in now_stat.items():
                pv = prev_stat.get(core)
                if pv and nv["total"] > pv["total"]:
                    dt, di = nv["total"] - pv["total"], nv["idle"] - pv["idle"]
                    idle_pct = round(di / dt * 100, 1) if dt > 0 else 0
                    if core == "cpu": cpu_total_idle_pct = idle_pct
                    else: per_core.append({"core": core, "idle_pct": idle_pct})
            per_core.sort(key=lambda x: int(x["core"].replace("cpu", "")) if x["core"].replace("cpu", "").isdigit() else 999)
        prev_stat = now_stat
    data["cpu_idle_pct"] = cpu_total_idle_pct
    data["per_core"] = per_core

    data["cpu_cores"] = cpuinfo.count("processor\t:")
    freqs = []
    if freq_raw:
        for f in freq_raw.split("\n"):
            try: freqs.append(int(f.strip()) / 1000)
            except: pass
    data["cpu_freq_avg"] = round(sum(freqs) / len(freqs), 0) if freqs else 0
    data["per_core_freqs"] = freqs

    fg_app = ""
    for line in fg_raw.split("\n"):
        if "mResumedActivity" in line or "mFocusedApp" in line:
            parts = line.split()
            for p in parts:
                if "/" in p and "." in p: fg_app = p.split("/")[0]; break
            if fg_app: break
    data["fg_app"] = fg_app[:50] if fg_app else "未知"

    # 单次 ps -A，含 %CPU,%MEM,RSS,TCNT,ARGS，一次解析出 CPU 和内存两份 Top15
    top_procs_d = {}; top_mem_d = {}
    for line in proc_raw.split("\n")[1:]:
        parts = line.strip().split(None, 4)
        if len(parts) >= 5:
            try:
                cpu = float(parts[0]); mem_pct = float(parts[1])
                rss = int(parts[2]); tcnt = int(parts[3])
                name = parts[4][:40]; pkg = parts[4].split(":")[0].split("/")[0].strip()[:50]
                if pkg not in top_procs_d:
                    top_procs_d[pkg] = {"cpu": cpu, "tcnt": tcnt, "name": name, "pkg": pkg}
                if pkg not in top_mem_d:
                    top_mem_d[pkg] = {"mem_pct": mem_pct, "rss": round(rss/1024,0), "tcnt": tcnt, "name": name, "pkg": pkg}
            except: pass
    data["top_procs"] = sorted(top_procs_d.values(), key=lambda x: x["cpu"], reverse=True)[:15]
    data["top_mem_procs"] = sorted(top_mem_d.values(), key=lambda x: x["mem_pct"], reverse=True)[:15]

    data["screen_on"] = "SCREEN_STATE_ON" in wm_raw
    for line in display_raw.split("\n"):
        if "fps=" in line and "activeModeId" not in line:
            for part in line.split(","):
                if "fps=" in part:
                    try: data["fps"] = part.split("=")[1].strip()
                    except: pass

    data["wakelocks"] = []; in_section = False
    for line in wl_raw.split("\n"):
        if "Wake Locks: size=" in line: in_section = True; continue
        if in_section and "=" in line:
            m2 = re.match(r'\s*Wake Lock (\S+)', line)
            if m2: data["wakelocks"].append(m2.group(1)[:40])
            if len(data["wakelocks"]) >= 5: break
    return data


def get_core_data():
    """快速返回核心指标：电池/温度/CPU/内存/屏幕。不包含进程列表，用于首次渲染。"""
    global prev_stat
    data = {"time": time.strftime("%H:%M:%S"), "timestamp": time.time()}

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_bat = ex.submit(adb, ["dumpsys", "battery"])
        f_batt_sys = ex.submit(adb, ["cat", "/sys/class/power_supply/battery/voltage_now",
            "/sys/class/power_supply/battery/current_now",
            "/sys/class/power_supply/battery/charge_type"])
        f_memhead = ex.submit(adb, ["cat /proc/meminfo | head -5"])
        f_stathead = ex.submit(adb, ["cat /proc/stat | head -5"])
        f_wm = ex.submit(adb, ["dumpsys", "window", "policy"])

        bat = f_bat.result(); batt_sys = f_batt_sys.result()
        mem_raw = f_memhead.result(); stat_raw = f_stathead.result()
        wm_raw = f_wm.result()

    for line in bat.split("\n"):
        line = line.strip()
        if "temperature:" in line:
            try: data["temp"] = int(line.split(":")[1].strip()) / 10
            except: data["temp"] = 0
        elif "level:" in line:
            try: data["bat_level"] = int(line.split(":")[1].strip())
            except: data["bat_level"] = 0
        elif "status:" in line:
            try:
                s = int(line.split(":")[1].strip())
                data["charge_status"] = {2:"充电中",3:"放电中",4:"未充电",5:"已充满"}.get(s, "未知")
                data["charging"] = s == 2
            except: data["charge_status"] = "?"

    bs_lines = batt_sys.strip().split("\n")
    if len(bs_lines) >= 1:
        try: data["bat_voltage"] = int(bs_lines[0].strip()) / 1000000
        except: data["bat_voltage"] = 0
    if len(bs_lines) >= 2:
        try: curr_val = int(bs_lines[1].strip()); curr_ma = abs(curr_val) / 1000 if bs_lines[1].strip() else 0
        except: curr_ma = 0
    else: curr_ma = 0
    data["charge_current"] = round(curr_ma, 0)
    data["charge_power"] = round(curr_ma * data.get("bat_voltage", 0) / 1000, 1) if data.get("bat_voltage", 0) > 0 and curr_ma > 0 else 0

    mem = {}
    for line in mem_raw.split("\n"):
        parts = line.split(":")
        if len(parts) == 2:
            try: mem[parts[0].strip()] = int(parts[1].strip().split()[0])
            except: pass
    data["mem_total"] = round(mem.get("MemTotal", 0) / 1048576, 1)
    data["mem_avail"] = round(mem.get("MemAvailable", 0) / 1048576, 1)
    data["mem_used_pct"] = round((1 - mem.get("MemAvailable", 0) / max(mem.get("MemTotal", 1), 1)) * 100, 1)

    cpu_total_idle_pct = 0; per_core = []; now_stat = {}
    if stat_raw:
        for line in stat_raw.split("\n"):
            fields = line.split()
            if len(fields) >= 5 and fields[0].startswith("cpu"):
                vals = [int(x) for x in fields[1:8]]
                now_stat[fields[0]] = {"total": sum(vals), "idle": vals[3] + vals[4]}
    with prev_stat_lock:
        if prev_stat and now_stat:
            for core, nv in now_stat.items():
                pv = prev_stat.get(core)
                if pv and nv["total"] > pv["total"]:
                    dt, di = nv["total"] - pv["total"], nv["idle"] - pv["idle"]
                    idle_pct = round(di / dt * 100, 1) if dt > 0 else 0
                    if core == "cpu": cpu_total_idle_pct = idle_pct
                    else: per_core.append({"core": core, "idle_pct": idle_pct})
            per_core.sort(key=lambda x: int(x["core"].replace("cpu", "")) if x["core"].replace("cpu", "").isdigit() else 999)
        prev_stat = now_stat
    data["cpu_idle_pct"] = cpu_total_idle_pct
    data["per_core"] = per_core

    data["screen_on"] = "SCREEN_STATE_ON" in wm_raw
    return data


# ======================= v5 充电/内存/电池/省电 (保持不变) =======================

def charge_sample_thread():
    global charge_session
    while True:
        with charge_lock: active = charge_session["active"]
        if active:
            try:
                bat = adb(["dumpsys", "battery"])
                lvl = temp = volt = curr = 0
                for line in bat.split("\n"):
                    line = line.strip()
                    if "level:" in line: lvl = int(line.split(":")[1].strip())
                    elif "temperature:" in line: temp = int(line.split(":")[1].strip()) / 10
                try: volt = int(adb(["cat", "/sys/class/power_supply/battery/voltage_now"])) / 1000000
                except: pass
                try: curr = abs(int(adb(["cat", "/sys/class/power_supply/battery/current_now"]))) / 1000
                except: pass
                point = {"ts": time.time(), "time": time.strftime("%H:%M:%S"), "level": lvl,
                         "temp": round(temp,1), "voltage": round(volt,3), "current": round(curr,0),
                         "power": round(curr*volt/1000,1) if volt>0 and curr>0 else 0}
                with charge_lock:
                    charge_session["points"].append(point)
                    if lvl >= 100: _save_charge_session()
            except: pass
            time.sleep(1)
        else: time.sleep(2)


def start_charge_session(level):
    global charge_session
    with charge_lock:
        charge_session["active"] = True; charge_session["points"] = []
        charge_session["start_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        charge_session["start_level"] = level


def _save_charge_session():
    global charge_session
    now = time.strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(DATA_DIR, "charge_sessions", f"session_{now}.json")
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    session = {"start_time": charge_session["start_time"], "end_time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "start_level": charge_session["start_level"],
               "end_level": charge_session["points"][-1]["level"] if charge_session["points"] else 0,
               "points": charge_session["points"]}
    with open(fname, "w", encoding="utf-8") as f: json.dump(session, f, ensure_ascii=False)
    charge_session["active"] = False; charge_session["points"] = []


def get_charge_state():
    with charge_lock:
        return {"active": charge_session["active"], "start_time": charge_session["start_time"],
                "start_level": charge_session["start_level"], "point_count": len(charge_session["points"]),
                "points": list(charge_session["points"])}


def update_mem_history(top_mem_procs):
    t = time.time()
    with mem_lock:
        for p in top_mem_procs:
            pkg = p["pkg"]
            if pkg not in mem_history: mem_history[pkg] = collections.deque(maxlen=MEM_LEAK_WINDOW)
            mem_history[pkg].append((t, p["rss"]))


def check_mem_leaks():
    leaks = []
    with mem_lock:
        for pkg, samples in list(mem_history.items()):
            if len(samples) < 450: continue
            recent = list(samples)[-450:]; rss_vals = [s[1] for s in recent]
            if len(rss_vals) < 100: continue
            n = len(rss_vals); x_mean = (n-1)/2; y_mean = sum(rss_vals)/n
            num = sum((i-x_mean)*(rss_vals[i]-y_mean) for i in range(n))
            den = sum((i-x_mean)**2 for i in range(n))
            slope = num/den if den>0 else 0
            growth_pct = (rss_vals[-1]-rss_vals[0])/max(rss_vals[0],1)*100
            if slope>0.001 and growth_pct>15:
                leaks.append({"pkg":pkg,"start_rss":round(rss_vals[0]),"current_rss":round(rss_vals[-1]),
                              "growth_pct":round(growth_pct,1),"slope":round(slope,3)})
    leaks.sort(key=lambda x:x["growth_pct"], reverse=True)
    return leaks[:5]


def update_battery_health(force=False):
    global battery_health_data
    t = time.time()
    should_update = force or len(battery_health_data)==0
    if len(battery_health_data)>0:
        last_ts = battery_health_data[-1].get("ts",0) if isinstance(battery_health_data[-1],dict) else 0
        if t-last_ts>3600: should_update = True
    if not should_update: return
    cf = get_charge_full()
    if cf>0:
        hfile = os.path.join(DATA_DIR,"battery_health.json")
        with health_lock:
            entry = {"ts":t,"date":time.strftime("%Y-%m-%d %H:%M"),"charge_full":cf}
            battery_health_data.append(entry)
            if len(battery_health_data)>200: battery_health_data = battery_health_data[-200:]
            try:
                with open(hfile,"w",encoding="utf-8") as f: json.dump(battery_health_data,f,ensure_ascii=False)
            except: pass


def predict_battery_life():
    with health_lock: data = list(battery_health_data)
    if len(data)<3: return {"status":"need_more_data","message":"需要至少 3 条记录才能预测","records":len(data)}
    n = len(data); vals = [d["charge_full"] for d in data]
    x_mean = (n-1)/2; y_mean = sum(vals)/n
    num = sum((i-x_mean)*(vals[i]-y_mean) for i in range(n))
    den = sum((i-x_mean)**2 for i in range(n))
    slope = num/den if den>0 else 0; intercept = y_mean-slope*x_mean
    current_val = intercept+slope*(n-1); design_cap = max(vals)
    threshold = design_cap*0.8
    if slope>=0:
        return {"status":"stable","current":round(current_val),"design":design_cap,
                "degradation":round((1-current_val/design_cap)*100,1),"message":"容量稳定，未检测到衰减趋势"}
    days_to_80 = (threshold-current_val)/slope/24 if slope<0 else -1
    if days_to_80<0: days_to_80=9999
    return {"status":"degrading","current":round(current_val),"design":design_cap,
            "degradation":round((1-current_val/design_cap)*100,1),
            "slope_per_day":round(slope*24,2),"days_to_80pct":round(days_to_80,0),"records":n}


def handle_screen_off_power_save(screen_on, top_procs):
    global power_save_enabled, power_save_kill_log
    if not power_save_enabled or screen_on: return
    with power_save_lock:
        killed = []
        for p in top_procs:
            pkg = p["pkg"]
            if pkg in power_save_whitelist or pkg.startswith("com.android") or pkg.startswith("system"): continue
            if p["cpu"]>10:
                try:
                    subprocess.run([ADB,"shell","am","force-stop",pkg],capture_output=True,timeout=5)
                    killed.append(pkg)
                except: pass
        for pkg in killed:
            power_save_kill_log.append({"time":time.strftime("%H:%M:%S"),"pkg":pkg})
            if len(power_save_kill_log)>50: power_save_kill_log = power_save_kill_log[-50:]


def load_persisted_data():
    global battery_health_data, power_save_enabled, power_save_whitelist
    hfile = os.path.join(DATA_DIR,"battery_health.json")
    if os.path.exists(hfile):
        try:
            with open(hfile,"r",encoding="utf-8") as f: battery_health_data = json.load(f)
        except: pass
    psfile = os.path.join(DATA_DIR,"power_save.json")
    if os.path.exists(psfile):
        try:
            with open(psfile,"r",encoding="utf-8") as f:
                ps = json.load(f); power_save_enabled = ps.get("enabled",False)
                power_save_whitelist = set(ps.get("whitelist",[]))
        except: pass
    cf = get_charge_full()
    if cf>0: update_battery_health(force=True)


def save_power_save_state():
    psfile = os.path.join(DATA_DIR,"power_save.json")
    with power_save_lock: state = {"enabled":power_save_enabled,"whitelist":sorted(list(power_save_whitelist))}
    try:
        with open(psfile,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False)
    except: pass


# ======================= v6 - App 测试功能 =======================

def get_app_startup(pkg):
    """冷启动耗时测试 - am start -W"""
    # 先强杀
    subprocess.run([ADB,"shell","am","force-stop",pkg],capture_output=True,timeout=5)
    time.sleep(0.5)
    # 解析 launcher activity
    launcher = None
    raw = adb(["cmd","package","resolve-activity","--brief","-c","android.intent.category.LAUNCHER",pkg])
    if raw:
        for line in raw.strip().split("\n"):
            line = line.strip()
            if "/" in line and line.startswith(pkg):
                launcher = line
                break
    if not launcher:
        # 备用: monkey 解析
        raw = adb(["monkey","-p",pkg,"-c","android.intent.category.LAUNCHER","1"])
        for line in (raw or "").split("\n"):
            if "cmp=" in line:
                launcher = line.split("cmp=")[1].split()[0]
                break
    if launcher:
        raw = adb(["am","start","-W","-n",launcher])
    else:
        raw = ""
    result = {"pkg":pkg,"totalTime":0,"waitTime":0,"thisTime":0,"status":"ok"}
    for line in raw.split("\n"):
        if "TotalTime:" in line:
            try: result["totalTime"] = int(line.split(":")[1].strip())
            except: pass
        elif "WaitTime:" in line:
            try: result["waitTime"] = int(line.split(":")[1].strip())
            except: pass
        elif "ThisTime:" in line:
            try: result["thisTime"] = int(line.split(":")[1].strip())
            except: pass
        elif "Error" in line:
            result["status"] = line.strip()[:100]
    if result["totalTime"] == 0: result["status"] = "启动失败，请确认包名和 MainActivity"
    else:
        with startup_lock:
            startup_history.append({"pkg":pkg,"total":result["totalTime"],"wait":result["waitTime"],
                                     "thisTime":result["thisTime"],"time":time.strftime("%H:%M:%S")})
    return result


def get_gfxinfo(pkg):
    """帧率与卡顿分析"""
    # 先重置统计
    adb(["dumpsys","gfxinfo",pkg,"reset"])
    raw = adb(["dumpsys","gfxinfo",pkg])
    result = {"pkg":pkg,"total_frames":0,"janky_frames":0,"percentile_99":0,
              "missed_vsync":0,"high_input_latency":0,"slow_ui":0,"slow_issue":0,"histogram":[]}
    in_profile = False
    for line in raw.split("\n"):
        line = line.strip()
        if "Stats since" in line: in_profile = True
        if in_profile:
            if "Total frames rendered:" in line:
                try: result["total_frames"] = int(line.split(":")[1].strip())
                except: pass
            elif "Janky frames:" in line:
                try: result["janky_frames"] = int(line.split(":")[1].strip().split("(")[0])
                except: pass
            elif "99th percentile:" in line:
                try: result["percentile_99"] = int(line.split(":")[1].strip().replace("ms",""))
                except: pass
            elif "Missed Vsync:" in line:
                try: result["missed_vsync"] = int(line.split(":")[1].strip())
                except: pass
            elif "High input latency:" in line:
                try: result["high_input_latency"] = int(line.split(":")[1].strip())
                except: pass
            elif "Slow UI thread:" in line:
                try: result["slow_ui"] = int(line.split(":")[1].strip())
                except: pass
            elif "Slow issue draw:" in line:
                try: result["slow_issue"] = int(line.split(":")[1].strip())
                except: pass
            elif "HISTOGRAM" in line: break
    # 解析直方图
    hist_start = raw.find("HISTOGRAM")
    if hist_start > 0:
        hist_section = raw[hist_start:]
        for line in hist_section.split("\n")[1:]:
            mh = re.match(r'\s*(\d+)ms\s*=\s*(\d+)', line.strip())
            if mh: result["histogram"].append({"ms": int(mh.group(1)), "count": int(mh.group(2))})
            else: break
    return result


def get_net_traffic():
    """获取所有 App 网络流量"""
    # 使用较长超时，dumpsys netstats 数据量大时很慢
    try:
        raw = subprocess.run([ADB, "shell", "dumpsys", "netstats", "detail"],
                             capture_output=True, text=True, timeout=15).stdout
    except:
        return []
    seen = {}
    # HyperOS/Android 13+ 格式：uid 在 ident 行，流量数据在后续 st= 行
    # ident=[{...}] uid=10354 set=DEFAULT tag=0x0
    #     NetworkStatsHistory: bucketDuration=7200
    #         st=1783188000 rb=4649 rp=12 tb=2633 tp=13 op=0
    current_uid = None
    for line in raw.split("\n"):
        m_uid = re.search(r'uid=(\d+)', line)
        if m_uid:
            uid_val = int(m_uid.group(1))
            # 只统计有效 uid（排除 uid=-1 设备汇总和 uid=0 系统），且需要 tag=0x0
            if uid_val > 0 and "tag=0x0" in line:
                current_uid = uid_val
            else:
                current_uid = None
            continue
        if current_uid is None:
            continue
        rb = re.search(r'rb=(\d+)', line)
        rp = re.search(r'rp=(\d+)', line)
        tb = re.search(r'tb=(\d+)', line)
        tp = re.search(r'tp=(\d+)', line)
        if not (rb or rp or tb or tp):
            continue
        rx = (int(rb.group(1)) if rb else 0) + (int(rp.group(1)) if rp else 0)
        tx = (int(tb.group(1)) if tb else 0) + (int(tp.group(1)) if tp else 0)
        if rx == 0 and tx == 0:
            continue
        if current_uid in seen:
            seen[current_uid]["rx"] += rx; seen[current_uid]["tx"] += tx
        else:
            seen[current_uid] = {"uid": current_uid, "rx": rx, "tx": tx, "pkg": f"uid_{current_uid}"}
    # 解析 uid → 包名
    try:
        pkg_raw = subprocess.run([ADB, "shell", "pm", "list", "packages", "-U"],
                                 capture_output=True, text=True, timeout=10).stdout
    except:
        pkg_raw = ""
    uid_to_pkg = {}
    for line in pkg_raw.split("\n"):
        mp = re.match(r'package:(\S+)\s+uid:(\d+)', line)
        if mp: uid_to_pkg[int(mp.group(2))] = mp.group(1)
    apps = []
    for uid, info in seen.items():
        info["pkg"] = uid_to_pkg.get(uid, info["pkg"])
        total_kb = (info["rx"] + info["tx"]) / 1024
        apps.append({"pkg": info["pkg"], "uid": uid,
                     "rx_kb": round(info["rx"]/1024, 1),
                     "tx_kb": round(info["tx"]/1024, 1),
                     "total_kb": round(total_kb, 1)})
    apps.sort(key=lambda x: x["total_kb"], reverse=True)
    return apps[:20]


def start_logcat_stream(pkg):
    """启动 logcat 流"""
    pid = ""
    if pkg:
        ps_raw = adb(["shell","pidof",pkg])
        if ps_raw:
            pids = ps_raw.strip().split()
            if pids: pid = pids[0]

    stream_id = str(uuid.uuid4())[:8]
    q = queue.Queue()
    stop = threading.Event()

    if pid:
        cmd = [ADB, "shell", "logcat", "-v", "brief", "--pid=" + pid]
    else:
        cmd = [ADB, "shell", "logcat", "-v", "brief"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except:
        return None

    with logcat_lock:
        logcat_streams[stream_id] = {"proc": proc, "queue": q, "stop": stop, "pkg": pkg}

    def reader():
        try:
            for line in proc.stdout:
                if stop.is_set(): break
                line = line.strip()
                if line:
                    # 解析日志等级
                    level = "V"
                    for l in ["F/","E/","W/","I/","D/","V/"]:
                        if f" {l}" in line or line.startswith(l):
                            level = l[0]; break
                    q.put({"time": time.strftime("%H:%M:%S"), "level": level, "msg": line})
        except: pass
        finally:
            proc.terminate()
            with logcat_lock: logcat_streams.pop(stream_id, None)

    threading.Thread(target=reader, daemon=True).start()
    return stream_id


def stop_logcat_stream(stream_id):
    with logcat_lock:
        s = logcat_streams.get(stream_id)
        if s: s["stop"].set()
    return True


def start_monkey(pkg, count=1000, throttle=200):
    """启动 Monkey 测试"""
    stream_id = str(uuid.uuid4())[:8]
    q = queue.Queue()
    stop = threading.Event()
    cmd = [ADB, "shell", "monkey", "-p", pkg, "--throttle", str(throttle),
           "--ignore-crashes", "--ignore-timeouts", "--ignore-security-exceptions",
           "-v", str(count)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except:
        return None
    with monkey_lock:
        monkey_streams[stream_id] = {"proc": proc, "queue": q, "stop": stop, "pkg": pkg,
                                      "count": count, "started": time.strftime("%H:%M:%S")}

    def reader():
        lines = []; events_injected = 0; crashes = 0; anrs = 0
        try:
            for line in proc.stdout:
                if stop.is_set(): break
                line = line.strip()
                if line:
                    q.put({"time": time.strftime("%H:%M:%S"), "msg": line})
                    lines.append(line)
                    if "Events injected:" in line:
                        try: events_injected = int(line.split(":")[1].strip())
                        except: pass
                    if "// CRASH:" in line: crashes += 1
                    if "// NOT RESPONDING:" in line: anrs += 1
        except: pass
        finally:
            proc.terminate()
            # 汇总结果
            summary = "\n".join(lines[-30:])
            # 解析最终统计
            for l in lines:
                if "Events injected:" in l:
                    try: events_injected = int(l.split(":")[1].strip())
                    except: pass
                if "// Monkey finished" in l: break
            with monkey_results_lock:
                monkey_results.append({"pkg": pkg, "time": time.strftime("%H:%M:%S"),
                                        "events": events_injected, "crashes": crashes, "anrs": anrs,
                                        "total": count})
            q.put({"done":True, "events":events_injected, "crashes":crashes, "anrs":anrs, "total":count})
            with monkey_lock: monkey_streams.pop(stream_id, None)

    threading.Thread(target=reader, daemon=True).start()
    return stream_id


def stop_monkey(stream_id):
    with monkey_lock:
        s = monkey_streams.get(stream_id)
        if s:
            s["stop"].set()
            # 直接杀掉设备端和本地的 monkey 进程，否则 stop 标记无法中断阻塞的 stdout 读取
            subprocess.run([ADB, "shell", "killall", "monkey"], capture_output=True, timeout=3)
            try: s["proc"].terminate()
            except: pass
    return True


def take_screenshot():
    """截图并拉到本地"""
    shot_id = str(uuid.uuid4())[:8]
    remote = "/sdcard/monitor_screen.png"
    local = os.path.join(DATA_DIR, "screenshots", f"shot_{shot_id}.png")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    subprocess.run([ADB, "shell", "screencap", "-p", remote], capture_output=True, timeout=5)
    subprocess.run([ADB, "pull", remote, local], capture_output=True, timeout=5)
    subprocess.run([ADB, "shell", "rm", remote], capture_output=True, timeout=2)
    with screenshot_lock:
        screenshot_store[shot_id] = local
    return {"id": shot_id, "path": local, "time": time.strftime("%H:%M:%S")}


# ======================= 服务重启 =======================

SCRIPT_PATH = os.path.abspath(__file__)
CURRENT_PID = os.getpid()

def do_restart():
    bat = f'''@echo off
timeout /t 2 /nobreak >nul
taskkill /f /pid {CURRENT_PID} >nul 2>&1
start "" python "{SCRIPT_PATH}"
del "%~f0"
'''
    fd, path = tempfile.mkstemp(suffix=".bat", prefix="monitor_restart_")
    with os.fdopen(fd, "w") as f: f.write(bat)
    subprocess.Popen(["cmd", "/c", path], creationflags=subprocess.CREATE_NEW_CONSOLE | 0x00000008)


# ======================= HTML 模板 =======================

HTML_TPL = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>手机实时监控 v6</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Consolas','Microsoft YaHei',monospace; padding: 16px; }
h1 { font-size: 15px; margin-bottom: 6px; color: #58a6ff; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.btn { font-size: 9px; padding: 3px 10px; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; border-radius: 4px; cursor: pointer; }
.btn:hover { background: #30363d; }
.btn-on { background: #2ea043; border-color: #2ea043; }
.btn-off { background: #F44336; border-color: #F44336; }
.tabs { display: flex; gap: 2px; margin-bottom: 10px; border-bottom: 1px solid #30363d; flex-wrap: wrap; }
.tab { font-size: 9px; padding: 5px 12px; background: transparent; color: #8b949e; border: none; cursor: pointer; border-bottom: 2px solid transparent; }
.tab:hover { color: #c9d1d9; }
.tab.active { color: #58a6ff; border-bottom-color: #58a6ff; }
.tab.app { color: #d2a8ff; }
.tab.app.active { color: #d2a8ff; border-bottom-color: #d2a8ff; }
.tab-sep { width: 1px; background: #30363d; margin: 4px 6px; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; margin-bottom: 8px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 7px 9px; margin-bottom: 8px; }
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
.footer { text-align: center; font-size: 8px; color: #484f58; margin-top: 10px; }
.log-view { background: #0a0e14; border: 1px solid #30363d; border-radius: 5px; height: 400px; overflow-y: auto; padding: 8px; font-size: 10px; font-family: 'Consolas', monospace; line-height: 1.4; }
.log-line { white-space: nowrap; }
.log-V { color: #8b949e; } .log-D { color: #58a6ff; } .log-I { color: #4CAF50; }
.log-W { color: #FF9800; } .log-E { color: #F44336; } .log-F { color: #F44336; font-weight: bold; }
.log-time { color: #484f58; margin-right: 6px; }
input[type="text"], input[type="number"] { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 6px; font-size: 9px; border-radius: 3px; font-family: inherit; width: 160px; }
input[type="number"] { width: 70px; }
.add-btn { font-size: 9px; padding: 2px 8px; background: #2ea043; border: none; color: #fff; border-radius: 3px; cursor: pointer; }
.remove-btn { font-size: 8px; color: #F44336; cursor: pointer; margin-left: 6px; }
.whitelist-item { display: inline-block; font-size: 8px; background: #21262d; padding: 2px 6px; border-radius: 3px; margin: 2px; }
.screenshot-preview { max-width: 280px; border: 1px solid #30363d; border-radius: 3px; cursor: pointer; }
.form-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.monkey-status { font-size: 12px; padding: 5px 12px; border-radius: 4px; display: inline-block; }
.monkey-running { background: #FF9800; color: #000; }
.monkey-done { background: #4CAF50; color: #fff; }
.hist-bar { display: inline-block; height: 14px; background: #58a6ff; border-radius: 2px; vertical-align: middle; margin-right: 4px; min-width: 2px; }
</style>
</head>
<body>
<h1>手机实时监控 v6
<span style="flex:1"></span>
<button class="btn" onclick="exportCSV()">CSV导出</button>
<button class="btn" onclick="restartServer()">重启服务</button>
</h1>
<div class="tabs">
<button class="tab active" onclick="switchTab('dashboard')">仪表盘</button>
<button class="tab" onclick="switchTab('charge')">充电曲线</button>
<button class="tab" onclick="switchTab('wakeup')">唤醒源</button>
<button class="tab" onclick="switchTab('leaks')">内存泄漏</button>
<button class="tab" onclick="switchTab('battery')">电池寿命</button>
<button class="tab" onclick="switchTab('powersave')">省电策略</button>
<span class="tab-sep"></span>
<button class="tab app" onclick="switchTab('startup')">冷启动</button>
<button class="tab app" onclick="switchTab('logcat')">Logcat</button>
<button class="tab app" onclick="switchTab('gfx')">帧率</button>
<button class="tab app" onclick="switchTab('traffic')">流量</button>
<button class="tab app" onclick="switchTab('monkey')">Monkey</button>
<button class="tab app" onclick="switchTab('screenshot')">截图对比</button>
</div>

<!-- 仪表盘 -->
<div id="tab-dashboard" class="tab-content active"><div id="dash"></div></div>
<div id="tab-charge" class="tab-content"><div id="charge"></div></div>
<div id="tab-wakeup" class="tab-content"><div id="wakeup"></div></div>
<div id="tab-leaks" class="tab-content"><div id="leaks"></div></div>
<div id="tab-battery" class="tab-content"><div id="battery-health"></div></div>
<div id="tab-powersave" class="tab-content"><div id="powersave"></div></div>

<!-- v6 App 测试 -->
<div id="tab-startup" class="tab-content">
<div class="card"><div class="card-title">App 冷启动耗时测试</div>
<div class="form-row">
<input type="text" id="startupPkg" placeholder="包名，如 com.tencent.mm" value="com.tencent.mm">
<button class="btn add-btn" onclick="testStartup()">测试启动</button>
</div>
<div id="startupResult"></div>
<div id="startupHistory"></div>
</div>
</div>

<div id="tab-logcat" class="tab-content">
<div class="card"><div class="card-title">Logcat 实时日志</div>
<div class="form-row">
<input type="text" id="logcatPkg" placeholder="包名（可选，过滤PID）">
<button class="btn add-btn" id="logcatStartBtn" onclick="toggleLogcat()">开始</button>
<button class="btn" onclick="clearLogcat()">清屏</button>
</div>
<div class="log-view" id="logcatView"><span style="color:#484f58">点击"开始"启动实时日志流</span></div>
</div>
</div>

<div id="tab-gfx" class="tab-content">
<div class="card"><div class="card-title">帧率与卡顿分析 (dumpsys gfxinfo)</div>
<div class="form-row">
<input type="text" id="gfxPkg" placeholder="包名">
<button class="btn add-btn" onclick="queryGfx()">查询</button>
<span style="font-size:8px;color:#8b949e">先打开目标 App 操作几秒后查询</span>
</div>
<div id="gfxResult"></div>
</div>
</div>

<div id="tab-traffic" class="tab-content">
<div class="card"><div class="card-title">App 网络流量排行</div>
<button class="btn add-btn" style="margin-bottom:6px" onclick="queryTraffic()">刷新流量统计</button>
<div id="trafficResult"></div>
</div>
</div>

<div id="tab-monkey" class="tab-content">
<div class="card"><div class="card-title">Monkey 压测</div>
<div class="form-row">
<input type="text" id="monkeyPkg" placeholder="包名" value="com.tencent.mm">
<input type="number" id="monkeyCount" placeholder="事件数" value="500" style="width:70px">
<input type="number" id="monkeyThrottle" placeholder="间隔ms" value="200" style="width:70px">
<button class="btn add-btn" id="monkeyBtn" onclick="toggleMonkey()">开始压测</button>
</div>
<div id="monkeyStatus"></div>
<div id="monkeyHistory" style="margin-top:8px"></div>
<div class="log-view" id="monkeyView" style="margin-top:6px;height:300px"><span style="color:#484f58">日志输出...</span></div>
</div>
</div>

<div id="tab-screenshot" class="tab-content">
<div class="card"><div class="card-title">截图对比</div>
<div class="form-row">
<button class="btn add-btn" onclick="takeShot('a')">截图 A</button>
<button class="btn add-btn" onclick="takeShot('b')">截图 B</button>
<button class="btn" onclick="compareShots()" id="compareBtn" disabled>对比</button>
<button class="btn" onclick="resetShots()">重置</button>
</div>
<div class="row">
<div class="col"><div class="card-title" style="margin-bottom:4px">截图 A</div><div id="shotA" style="color:#484f58;font-size:10px">未截取</div></div>
<div class="col"><div class="card-title" style="margin-bottom:4px">截图 B</div><div id="shotB" style="color:#484f58;font-size:10px">未截取</div></div>
</div>
<div style="margin-top:8px"><div class="card-title" style="margin-bottom:4px">差异图（红色=差异区域）</div><canvas id="diffCanvas" style="max-width:100%;display:none"></canvas><div id="diffInfo" style="font-size:10px;color:#8b949e"></div></div>
</div>
</div>

<div class="toast" id="toast"></div>
<div class="footer">Marvis Phone Monitor v6</div>

<script>
const MAX_HISTORY = 30;
const ALERT_THRESHOLD = 20, ALERT_SAMPLES = 5;
let history = [], alertTracker = {}, currentTab = 'dashboard';
let chargePoints = [], prevCharging = false, prevScreenOn = true;
let logcatActive = false, monkeyActive = false, logcatEs = null, monkeyEs = null;
let shotIdA = null, shotIdB = null;

if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();

function pc(pct) { return pct < 50 ? "#4CAF50" : pct < 75 ? "#FF9800" : "#F44336"; }
function tc(t) { return t < 35 ? "#4CAF50" : t < 40 ? "#FF9800" : "#F44336"; }
function extractPkg(name) { let n = (name||"").trim(); let m = n.match(/^([a-z][a-z0-9_.]*[a-z0-9])/); return m ? m[1] : ""; }

function showToast(msg, bg) {
    let t = document.getElementById('toast'); t.textContent = msg; t.style.background = bg || '#F44336'; t.style.color = '#fff';
    t.style.display = 'block'; setTimeout(() => { t.style.display = 'none'; }, 2000);
}

function switchTab(name) {
    currentTab = name;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    let target = document.querySelector('.tab[onclick*="' + name + '"]');
    if (target) target.classList.add('active');
    let content = document.getElementById('tab-' + name);
    if (content) content.classList.add('active');
    if (name === 'charge') renderChargeChart();
    if (name === 'leaks') loadLeaks();
    if (name === 'wakeup') loadWakeup();
    if (name === 'battery') loadBatteryHealth();
    if (name === 'powersave') loadPowerSave();
    if (name === 'traffic') queryTraffic();
    if (name === 'monkey') loadMonkeyHistory();
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
        if (v >= ALERT_SAMPLES) { alertTracker[k] = -999; showToast('⚠ ' + (seen[k]||{}).name + ' CPU高负载', '#F44336'); }
    });
}

function drawChart(history, canvasId, key, color, label, maxVal) {
    let c = document.getElementById(canvasId); if (!c) return;
    let ctx = c.getContext('2d'), W = Math.max(c.parentElement.clientWidth, 200), H = 90;
    c.width = W*2; c.height = H*2; c.style.width = W+'px'; c.style.height = H+'px'; ctx.scale(2,2);
    ctx.fillStyle = '#161b22'; ctx.fillRect(0,0,W,H);
    if (history.length < 2) return;
    let vals = history.map(d => d[key] || 0), mx = maxVal || Math.max(1,...vals);
    ctx.strokeStyle = '#21262d'; ctx.lineWidth = 0.5;
    for (let i=0;i<=4;i++) { let y=8+(H-16)*i/4; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    let step = W / Math.max(history.length-1,1);
    for (let i=0;i<history.length;i++) {
        let x=i*step, y=8+(H-16)*(1-vals[i]/mx);
        if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    } ctx.stroke();
}

// ===== 仪表盘渲染 =====
let dashFirstPaint = true;

function buildProcRows(topProcs, topMemProcs) {
    let procRows=""; (topProcs||[]).forEach((p,i)=>{
        const clr=p.cpu<5?"#4CAF50":p.cpu<15?"#FF9800":"#F44336";
        let pkg=p.pkg||extractPkg(p.name), killBtn=pkg?'<span class="kill-btn" onclick="killProcess(\''+pkg+'\',this)">X</span>':'';
        procRows+="<tr><td>"+(i+1)+"</td><td><span class='process-name'>"+p.name+"</span>"+killBtn+"</td><td style='text-align:center'>"+(p.tcnt||0)+"</td><td style='color:"+clr+";font-weight:bold'>"+p.cpu.toFixed(1)+"%</td></tr>";
    });
    let memProcRows=""; (topMemProcs||[]).forEach((p,i)=>{
        const clr=p.mem_pct<2?"#4CAF50":p.mem_pct<5?"#FF9800":"#F44336";
        let pkg=p.pkg||extractPkg(p.name), killBtn=pkg?'<span class="kill-btn" onclick="killProcess(\''+pkg+'\',this)">X</span>':'';
        memProcRows+="<tr><td>"+(i+1)+"</td><td><span class='process-name'>"+p.name+"</span>"+killBtn+"</td><td>"+p.mem_pct.toFixed(1)+"%</td><td>"+p.rss+" MB</td></tr>";
    });
    return [procRows, memProcRows];
}

function renderDash(d) {
    const batPct = d.bat_level||0;
    const batClr = batPct>50?"#4CAF50":batPct>20?"#FF9800":"#F44336";
    const chgClr = d.charging?"#4CAF50":"#484f58";
    const chgArrow = d.charging?"↑":(d.charge_status=="放电中"?"↓":"");
    let wlTags = ""; (d.wakelocks||[]).forEach(w=>{wlTags+='<span class="wl-tag">'+w+'</span>';});
    if(!wlTags) wlTags='<span style="color:#484f58;font-size:8px">无</span>';
    let coreBars=""; (d.per_core_freqs||[]).forEach((f,i)=>{
        let pct=Math.min(f/3000*100,100),clr=f>2000?"#F44336":f>1000?"#FF9800":"#4CAF50";
        coreBars+='<span class="core-label">C'+i+'</span><span class="core-bar"><span class="core-bar-fill" style="width:'+pct+'%;background:'+clr+'"></span></span>';
    });
    let [procRows, memProcRows] = buildProcRows(d.top_procs, d.top_mem_procs);
    let html='<div class="grid">';
    html+='<div class="card"><div class="card-title">温度</div><div class="big-num" style="color:'+tc(d.temp)+'" id="val-temp">'+(d.temp||0).toFixed(1)+'<span class="unit">°C</span></div></div>';
    html+='<div class="card"><div class="card-title">SoC</div><div class="big-num" style="color:'+tc(d.soc_max)+'" id="val-soc">'+(d.soc_max||0).toFixed(1)+'<span class="unit">°C</span></div></div>';
    html+='<div class="card"><div class="card-title">电量</div><div class="big-num" style="color:'+batClr+'" id="val-bat">'+batPct+'<span class="unit">%</span></div></div>';
    html+='<div class="card"><div class="card-title">充/放电</div><div class="num-sm" style="color:'+chgClr+'" id="val-charge">'+chgArrow+' '+(d.charge_current||0)+'<span class="unit">mA</span></div><div class="label" id="val-power">'+(d.charge_power||0)+'W</div></div>';
    html+='<div class="card"><div class="card-title">内存</div><div class="big-num" style="color:'+pc(d.mem_used_pct)+'" id="val-mem">'+(d.mem_used_pct||0).toFixed(1)+'<span class="unit">%</span></div></div>';
    html+='<div class="card"><div class="card-title">CPU空闲</div><div class="num-sm" id="val-cpu">'+(d.cpu_idle_pct||0).toFixed(0)+'<span class="unit">%</span></div></div>';
    html+='<div class="card"><div class="card-title">前台</div><div class="num-sm" id="val-fg" style="font-size:10px">'+(d.fg_app||"")+'</div></div>';
    html+='<div class="card"><div class="card-title">屏幕</div><div id="val-screen" style="font-size:10px">'+(d.screen_on?'<span style="color:#58a6ff">亮屏</span>':'<span style="color:#484f58">息屏</span>')+'</div></div>';
    html+='<div class="card"><div class="card-title">时间</div><div class="num-sm" id="val-time" style="color:#58a6ff">'+d.time+'</div></div>';
    html+='</div>';
    html+='<div class="card" style="margin-bottom:8px"><div class="card-title">核心频率</div><div id="core-bars" style="line-height:18px">'+coreBars+'</div></div>';
    html+='<div class="chart-card"><div class="chart-title">历史趋势</div><div class="row"><div class="col"><canvas id="chart_temp"></canvas></div><div class="col"><canvas id="chart_discharge"></canvas></div></div></div>';
    html+='<div class="card" style="margin-bottom:6px"><div class="card-title">唤醒锁</div><div id="wl-tags">'+wlTags+'</div></div>';
    html+='<div class="row"><div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>线程</th><th>CPU%</th></tr><tbody id="proc-cpu-tbody">'+procRows+'</tbody></table></div></div>';
    html+='<div class="col"><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>内存%</th><th>RSS</th></tr><tbody id="proc-mem-tbody">'+memProcRows+'</tbody></table></div></div></div>';
    document.getElementById("dash").innerHTML = html;
}

function patchDash(d) {
    let batPct = d.bat_level||0;
    let batClr = batPct>50?"#4CAF50":batPct>20?"#FF9800":"#F44336";
    let chgClr = d.charging?"#4CAF50":"#484f58";
    let chgArrow = d.charging?"↑":(d.charge_status=="放电中"?"↓":"");
    // 卡片值
    let vt=document.getElementById("val-temp"); if(vt){vt.style.color=tc(d.temp);vt.innerHTML=(d.temp||0).toFixed(1)+'<span class="unit">°C</span>';}
    let vs=document.getElementById("val-soc"); if(vs){vs.style.color=tc(d.soc_max);vs.innerHTML=(d.soc_max||0).toFixed(1)+'<span class="unit">°C</span>';}
    let vb=document.getElementById("val-bat"); if(vb){vb.style.color=batClr;vb.innerHTML=batPct+'<span class="unit">%</span>';}
    let vc=document.getElementById("val-charge"); if(vc){vc.style.color=chgClr;vc.innerHTML=chgArrow+' '+(d.charge_current||0)+'<span class="unit">mA</span>';}
    let vp=document.getElementById("val-power"); if(vp)vp.innerHTML=(d.charge_power||0)+'W';
    let vm=document.getElementById("val-mem"); if(vm){vm.style.color=pc(d.mem_used_pct);vm.innerHTML=(d.mem_used_pct||0).toFixed(1)+'<span class="unit">%</span>';}
    let vcpu=document.getElementById("val-cpu"); if(vcpu)vcpu.innerHTML=(d.cpu_idle_pct||0).toFixed(0)+'<span class="unit">%</span>';
    let vfg=document.getElementById("val-fg"); if(vfg)vfg.innerHTML=d.fg_app||"";
    let vsc=document.getElementById("val-screen"); if(vsc)vsc.innerHTML=d.screen_on?'<span style="color:#58a6ff">亮屏</span>':'<span style="color:#484f58">息屏</span>';
    let vti=document.getElementById("val-time"); if(vti)vti.innerHTML=d.time;
    // 核心频率
    let coreBars=""; (d.per_core_freqs||[]).forEach((f,i)=>{
        let pct=Math.min(f/3000*100,100),clr=f>2000?"#F44336":f>1000?"#FF9800":"#4CAF50";
        coreBars+='<span class="core-label">C'+i+'</span><span class="core-bar"><span class="core-bar-fill" style="width:'+pct+'%;background:'+clr+'"></span></span>';
    });
    let cb = document.getElementById("core-bars"); if(cb) cb.innerHTML = coreBars;
    // 唤醒锁
    let wlTags = ""; (d.wakelocks||[]).forEach(w=>{wlTags+='<span class="wl-tag">'+w+'</span>';});
    if(!wlTags) wlTags='<span style="color:#484f58;font-size:8px">无</span>';
    let wt = document.getElementById("wl-tags"); if(wt) wt.innerHTML = wlTags;
    // 进程表
    let [procRows, memProcRows] = buildProcRows(d.top_procs, d.top_mem_procs);
    let cpuTbody = document.getElementById("proc-cpu-tbody"); if(cpuTbody) cpuTbody.innerHTML = procRows;
    let memTbody = document.getElementById("proc-mem-tbody"); if(memTbody) memTbody.innerHTML = memProcRows;
}

function renderChargeChart() { /* 保持原有 */ }

// ===== v5 标签页函数 =====
function loadWakeup() {
    fetch('/wakeup').then(r=>r.json()).then(data=>{
        let rows=data.map((s,i)=>'<tr><td>'+(i+1)+'</td><td>'+s.name+'</td><td>'+s.count+'</td><td>'+(s.ms>=60000?(s.ms/60000).toFixed(1)+'min':s.ms>=1000?(s.ms/1000).toFixed(1)+'s':s.ms+'ms')+'</td></tr>').join('');
        if(!rows)rows='<tr><td colspan="4" style="color:#484f58">未找到唤醒源数据</td></tr>';
        document.getElementById('wakeup').innerHTML='<div class="table-card"><table width="100%"><tr><th>#</th><th>唤醒源</th><th>次数</th><th>总时长</th></tr>'+rows+'</table></div>';
    });
}
function loadLeaks() {
    fetch('/leaks').then(r=>r.json()).then(data=>{
        let rows=data.map((l,i)=>'<tr><td>'+(i+1)+'</td><td>'+l.pkg+'</td><td>'+l.start_rss+'</td><td>'+l.current_rss+'</td><td style="color:'+(l.growth_pct>50?'#F44336':'#FF9800')+'">'+l.growth_pct+'%</td><td>'+l.slope+'</td></tr>').join('');
        if(!rows)rows='<tr><td colspan="6" style="color:#4CAF50">未检测到疑似内存泄漏</td></tr>';
        document.getElementById('leaks').innerHTML='<div class="card"><div class="card-title">内存泄漏检测 - RSS持续增长>15% 超过15分钟</div></div><div class="table-card"><table width="100%"><tr><th>#</th><th>进程</th><th>起始RSS</th><th>当前RSS</th><th>增长</th><th>速率</th></tr>'+rows+'</table></div>';
    });
}
function loadBatteryHealth() {
    fetch('/battery_health').then(r=>r.json()).then(data=>{
        if(data.status==='need_more_data'){document.getElementById('battery-health').innerHTML='<div class="card"><div class="card-title">电池寿命预测</div><div style="padding:20px;color:#8b949e">'+data.message+'（当前'+data.records+'条）</div></div>';return;}
        let daysClr=data.days_to_80pct<90?'#F44336':data.days_to_80pct<365?'#FF9800':'#4CAF50';
        document.getElementById('battery-health').innerHTML='<div class="grid">'+
        '<div class="card"><div class="card-title">当前容量</div><div class="big-num" style="color:#58a6ff">'+data.current+'<span class="unit">mAh</span></div></div>'+
        '<div class="card"><div class="card-title">设计容量</div><div class="big-num" style="color:#8b949e">'+data.design+'<span class="unit">mAh</span></div></div>'+
        '<div class="card"><div class="card-title">已衰减</div><div class="big-num" style="color:'+(data.degradation>20?'#F44336':'#FF9800')+'">'+data.degradation+'<span class="unit">%</span></div></div>'+
        '<div class="card"><div class="card-title">衰减速率</div><div class="num-sm">'+data.slope_per_day+'<span class="unit">mAh/天</span></div></div>'+
        '<div class="card"><div class="card-title">预计可用</div><div class="big-num" style="color:'+daysClr+'">'+(data.days_to_80pct<9998?data.days_to_80pct:'>10000')+'<span class="unit">天</span></div></div>'+
        '</div>';
    });
}
function loadPowerSave() {
    fetch('/powersave').then(r=>r.json()).then(data=>{
        let toggleBtn=data.enabled?'<button class="btn btn-on" onclick="togglePowerSave()">已开启 - 点击关闭</button>':'<button class="btn btn-off" onclick="togglePowerSave()">已关闭 - 点击开启</button>';
        let whItems="";(data.whitelist||[]).forEach(w=>{whItems+='<span class="whitelist-item">'+w+'<span class="remove-btn" onclick="removeWhitelist(\''+w+'\')">✕</span></span>';});
        let killRows="";(data.kill_log||[]).slice().reverse().forEach(k=>{killRows+='<tr><td>'+k.time+'</td><td>'+k.pkg+'</td></tr>';});
        document.getElementById('powersave').innerHTML='<div class="card"><div class="card-title">息屏省电策略</div><div style="margin:8px 0">'+toggleBtn+'</div></div>'+
        '<div class="card"><div class="card-title">白名单</div><div>'+whItems+'</div><input type="text" id="whInput" placeholder="包名"><button class="add-btn" onclick="addWhitelist()">添加</button></div>'+
        '<div class="table-card"><table width="100%"><tr><th>时间</th><th>已杀进程</th></tr>'+killRows+'</table></div>';
    });
}
function togglePowerSave(){fetch('/powersave/toggle',{method:'POST'}).then(()=>loadPowerSave());}
function addWhitelist(){let v=document.getElementById('whInput').value.trim();if(v)fetch('/powersave/whitelist/add?pkg='+encodeURIComponent(v),{method:'POST'}).then(()=>loadPowerSave());}
function removeWhitelist(pkg){fetch('/powersave/whitelist/remove?pkg='+encodeURIComponent(pkg),{method:'POST'}).then(()=>loadPowerSave());}

// ===== v6 - 冷启动测试 =====
function testStartup() {
    let pkg = document.getElementById('startupPkg').value.trim();
    if(!pkg) return showToast('请输入包名');
    document.getElementById('startupResult').innerHTML = '<div style="color:#FF9800;padding:10px">测试中...</div>';
    fetch('/startup?pkg='+encodeURIComponent(pkg),{method:'POST'}).then(r=>r.json()).then(d=>{
        if(d.status==='ok'){
            document.getElementById('startupResult').innerHTML=
            '<div class="grid" style="margin-top:8px">'+
            '<div class="card"><div class="card-title">TotalTime</div><div class="big-num" style="color:#58a6ff">'+d.totalTime+'<span class="unit">ms</span></div></div>'+
            '<div class="card"><div class="card-title">WaitTime</div><div class="big-num" style="color:#FF9800">'+d.waitTime+'<span class="unit">ms</span></div></div>'+
            '<div class="card"><div class="card-title">ThisTime</div><div class="big-num" style="color:#4CAF50">'+d.thisTime+'<span class="unit">ms</span></div></div>'+
            '</div>';
        } else {
            document.getElementById('startupResult').innerHTML='<div style="color:#F44336;padding:10px">'+d.status+'</div>';
        }
        loadStartupHistory();
    });
}
function loadStartupHistory() {
    fetch('/startup/history').then(r=>r.json()).then(data=>{
        let rows = data.map((h,i)=>'<tr><td>'+(i+1)+'</td><td>'+h.pkg+'</td><td>'+h.time+'</td><td>'+h.total+'ms</td><td>'+h.wait+'ms</td></tr>').join('');
        document.getElementById('startupHistory').innerHTML = rows ?
            '<div class="table-card" style="margin-top:8px"><table width="100%"><tr><th>#</th><th>包名</th><th>时间</th><th>Total</th><th>Wait</th></tr>'+rows+'</table></div>' : '';
    });
}

// ===== v6 - Logcat 实时流 =====
function toggleLogcat() {
    if(logcatActive){stopLogcat();return;}
    let pkg = document.getElementById('logcatPkg').value.trim();
    let url = '/logcat/stream';
    if(pkg) url += '?pkg='+encodeURIComponent(pkg);
    logcatActive = true;
    document.getElementById('logcatStartBtn').textContent = '停止';
    document.getElementById('logcatStartBtn').style.background = '#F44336';
    document.getElementById('logcatView').innerHTML = '';
    logcatEs = new EventSource(url);
    logcatEs.onmessage = function(e) {
        let data = JSON.parse(e.data);
        if(data.done){stopLogcat();return;}
        let view = document.getElementById('logcatView');
        let line = document.createElement('div');
        line.className = 'log-line log-'+data.level;
        line.innerHTML = '<span class="log-time">'+data.time+'</span>'+data.msg.replace(/</g,'&lt;').replace(/>/g,'&gt;');
        view.appendChild(line);
        view.scrollTop = view.scrollHeight;
        // 限制行数
        while(view.children.length > 500) view.removeChild(view.firstChild);
    };
    logcatEs.onerror = function(){stopLogcat();};
}
function stopLogcat() {
    logcatActive = false;
    document.getElementById('logcatStartBtn').textContent = '开始';
    document.getElementById('logcatStartBtn').style.background = '#2ea043';
    if(logcatEs){logcatEs.close();logcatEs=null;}
    fetch('/logcat/stop',{method:'POST'}).catch(()=>{});
}
function clearLogcat(){document.getElementById('logcatView').innerHTML='';}

// ===== v6 - 帧率分析 =====
function queryGfx() {
    let pkg = document.getElementById('gfxPkg').value.trim();
    if(!pkg) return showToast('请输入包名');
    document.getElementById('gfxResult').innerHTML = '<div style="color:#FF9800;padding:10px">查询中...</div>';
    fetch('/gfxinfo?pkg='+encodeURIComponent(pkg)).then(r=>r.json()).then(d=>{
        let jankPct = d.total_frames>0 ? (d.janky_frames/d.total_frames*100).toFixed(1) : 0;
        let histHtml = '';
        if(d.histogram && d.histogram.length>0){
            let maxCount = Math.max(...d.histogram.map(h=>h.count));
            histHtml = '<div style="margin-top:6px"><div class="card-title">帧耗时分布</div>';
            d.histogram.forEach(h=>{
                let w = Math.max(2, h.count/maxCount*200);
                histHtml += '<div style="font-size:8px;margin:1px 0"><span style="display:inline-block;width:30px;text-align:right">'+h.ms+'ms</span> <span class="hist-bar" style="width:'+w+'px"></span> '+h.count+'</div>';
            });
            histHtml += '</div>';
        }
        document.getElementById('gfxResult').innerHTML =
        '<div class="grid" style="margin-top:8px">'+
        '<div class="card"><div class="card-title">总帧数</div><div class="big-num" style="color:#58a6ff">'+d.total_frames+'</div></div>'+
        '<div class="card"><div class="card-title">Jank 帧</div><div class="big-num" style="color:'+(jankPct>10?'#F44336':'#4CAF50')+'">'+d.janky_frames+'<span class="unit"> ('+jankPct+'%)</span></div></div>'+
        '<div class="card"><div class="card-title">99分位</div><div class="big-num" style="color:'+(d.percentile_99>33?'#F44336':'#4CAF50')+'">'+d.percentile_99+'<span class="unit">ms</span></div></div>'+
        '<div class="card"><div class="card-title">Miss Vsync</div><div class="num-sm">'+d.missed_vsync+'</div></div>'+
        '<div class="card"><div class="card-title">高输入延迟</div><div class="num-sm">'+d.high_input_latency+'</div></div>'+
        '<div class="card"><div class="card-title">慢UI线程</div><div class="num-sm">'+d.slow_ui+'</div></div>'+
        '</div>'+histHtml;
    });
}

// ===== v6 - 网络流量 =====
function fmtSize(kb) {
    if (kb >= 1048576) return (kb / 1048576).toFixed(2) + ' GB';
    if (kb >= 1024) return (kb / 1024).toFixed(1) + ' MB';
    return kb.toFixed(1) + ' KB';
}

function queryTraffic() {
    document.getElementById('trafficResult').innerHTML = '<div style="color:#FF9800;padding:10px">查询中...</div>';
    fetch('/traffic').then(r=>r.json()).then(data=>{
        let rows = data.map((a,i)=>'<tr><td>'+(i+1)+'</td><td>'+a.pkg+'</td><td>'+fmtSize(a.rx_kb)+'</td><td>'+fmtSize(a.tx_kb)+'</td><td style="font-weight:bold">'+fmtSize(a.total_kb)+'</td></tr>').join('');
        document.getElementById('trafficResult').innerHTML =
        '<div class="table-card"><table width="100%"><tr><th>#</th><th>应用</th><th>接收</th><th>发送</th><th>总流量</th></tr>'+rows+'</table></div>';
    });
}

// ===== v6 - Monkey 压测 =====
function toggleMonkey() {
    if(monkeyActive){stopMonkey();return;}
    let pkg = document.getElementById('monkeyPkg').value.trim();
    let count = parseInt(document.getElementById('monkeyCount').value) || 500;
    let throttle = parseInt(document.getElementById('monkeyThrottle').value) || 200;
    if(!pkg) return showToast('请输入包名');
    monkeyActive = true;
    document.getElementById('monkeyBtn').textContent = '停止';
    document.getElementById('monkeyBtn').style.background = '#F44336';
    document.getElementById('monkeyStatus').innerHTML = '<div class="monkey-status monkey-running">运行中...</div>';
    document.getElementById('monkeyView').innerHTML = '';
    monkeyEs = new EventSource('/monkey/stream?pkg='+encodeURIComponent(pkg)+'&count='+count+'&throttle='+throttle);
    let lineCount = 0;
    monkeyEs.onmessage = function(e) {
        let data = JSON.parse(e.data);
        if(data.done){
            stopMonkey();
            document.getElementById('monkeyStatus').innerHTML = '<div class="monkey-status monkey-done">完成 - '+data.events+'/'+data.total+' 事件, '+data.crashes+' 崩溃</div>';
            loadMonkeyHistory();
            return;
        }
        let view = document.getElementById('monkeyView');
        let line = document.createElement('div');
        line.className = 'log-line';
        line.style.color = data.msg.includes('CRASH')?'#F44336':data.msg.includes('NOT RESPONDING')?'#FF9800':data.msg.includes('injected')?'#4CAF50':'#8b949e';
        line.textContent = data.msg;
        view.appendChild(line);
        lineCount++;
        if(lineCount > 300){view.removeChild(view.firstChild);lineCount--;}
        view.scrollTop = view.scrollHeight;
    };
    monkeyEs.onerror = function(){stopMonkey();};
}
function stopMonkey() {
    monkeyActive = false;
    document.getElementById('monkeyBtn').textContent = '开始压测';
    document.getElementById('monkeyBtn').style.background = '#2ea043';
    document.getElementById('monkeyStatus').innerHTML = '<div class="monkey-status monkey-stopped">已停止</div>';
    if(monkeyEs){monkeyEs.close();monkeyEs=null;}
    fetch('/monkey/stop',{method:'POST'}).catch(()=>{});
}
function loadMonkeyHistory() {
    fetch('/monkey/history').then(r=>r.json()).then(data=>{
        let rows = data.map((h,i)=>'<tr><td>'+(i+1)+'</td><td>'+h.pkg+'</td><td>'+h.time+'</td><td>'+h.events+'/'+h.total+'</td><td style="color:'+(h.crashes>0?'#F44336':'#4CAF50')+'">'+h.crashes+'</td></tr>').join('');
        document.getElementById('monkeyHistory').innerHTML = rows ?
        '<div class="table-card"><table width="100%"><tr><th>#</th><th>包名</th><th>时间</th><th>事件</th><th>崩溃</th></tr>'+rows+'</table></div>' : '';
    });
}

// ===== v6 - 截图对比 =====
function takeShot(slot) {
    fetch('/screenshot/take',{method:'POST'}).then(r=>r.json()).then(d=>{
        if(slot==='a'){shotIdA=d.id;document.getElementById('shotA').innerHTML='<div style="font-size:9px;color:#4CAF50">'+d.time+'</div><img class="screenshot-preview" src="/screenshot/'+d.id+'" onerror="this.style.display=\'none\'">';}
        else{shotIdB=d.id;document.getElementById('shotB').innerHTML='<div style="font-size:9px;color:#4CAF50">'+d.time+'</div><img class="screenshot-preview" src="/screenshot/'+d.id+'" onerror="this.style.display=\'none\'">';}
        document.getElementById('compareBtn').disabled = !(shotIdA && shotIdB);
    });
}
function compareShots() {
    if(!shotIdA||!shotIdB) return;
    let imgA = new Image(), imgB = new Image();
    imgA.src = '/screenshot/'+shotIdA;
    imgB.src = '/screenshot/'+shotIdB;
    let loaded = 0;
    function onLoad() {
        loaded++; if(loaded<2) return;
        let canvas = document.getElementById('diffCanvas');
        let w = Math.min(imgA.width, imgB.width);
        let h = Math.min(imgA.height, imgB.height);
        canvas.width = w; canvas.height = h;
        canvas.style.display = 'block';
        let ctx = canvas.getContext('2d');
        ctx.drawImage(imgA, 0, 0);
        let imgDataA = ctx.getImageData(0,0,w,h);
        ctx.clearRect(0,0,w,h);
        ctx.drawImage(imgB,0,0);
        let imgDataB = ctx.getImageData(0,0,w,h);
        let diffPixels = 0, totalPixels = w*h;
        for(let i=0;i<imgDataA.data.length;i+=4){
            let dr=Math.abs(imgDataA.data[i]-imgDataB.data[i]);
            let dg=Math.abs(imgDataA.data[i+1]-imgDataB.data[i+1]);
            let db=Math.abs(imgDataA.data[i+2]-imgDataB.data[i+2]);
            if(dr>30||dg>30||db>30){
                imgDataA.data[i]=255;imgDataA.data[i+1]=0;imgDataA.data[i+2]=0;imgDataA.data[i+3]=180;
                diffPixels++;
            }
        }
        ctx.putImageData(imgDataA,0,0);
        let diffPct = (diffPixels/totalPixels*100).toFixed(2);
        document.getElementById('diffInfo').innerHTML = '差异像素: '+diffPixels+' / '+totalPixels+' ('+diffPct+'%)';
        document.getElementById('diffCanvas').style.display = 'block';
    }
    imgA.onload = onLoad; imgB.onload = onLoad;
}
function resetShots(){shotIdA=null;shotIdB=null;document.getElementById('shotA').innerHTML='未截取';document.getElementById('shotB').innerHTML='未截取';document.getElementById('diffCanvas').style.display='none';document.getElementById('diffInfo').innerHTML='';document.getElementById('compareBtn').disabled=true;}

// ===== CSV导出 =====
function exportCSV() {
    if(history.length===0)return;
    let keys=['time','temp','soc_max','bat_level','bat_voltage','charge_current','charge_power','discharge_ma','mem_used_pct','swap_pct','disk_pct','cpu_freq_avg','cpu_idle_pct','screen_on','fg_app'];
    let csv='\uFEFF'+keys.join(',')+'\n';
    history.forEach(d=>{csv+=keys.map(k=>{let v=d[k];if(typeof v==='boolean')return v?'1':'0';return v!=null?v:'';}).join(',')+'\n';});
    let blob=new Blob([csv],{type:'text/csv;charset=utf-8'});let a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download='monitor_'+new Date().toISOString().slice(0,19).replace(/:/g,'-')+'.csv';a.click();URL.revokeObjectURL(a.href);
    showToast('CSV已导出','#4CAF50');
}
function restartServer(){if(!confirm('确定要重启监控服务吗?'))return;fetch('/restart',{method:'POST'}).then(r=>r.json()).then(d=>{showToast(d.message||'重启中...','#58a6ff');setTimeout(()=>{location.reload();},3000);}).catch(()=>{showToast('重启失败','#F44336');});}

// ===== 主循环 =====
let failCount=0;
function load(){
    fetch("/core").then(r=>r.json()).then(d=>{
        failCount=0;
        if(currentTab==='dashboard' && dashFirstPaint){renderDash(d);dashFirstPaint=false;}
    }).catch(()=>{failCount++;if(failCount>=3)document.getElementById("dash").innerHTML="ADB连接失败，请检查手机连接";});
    fetch("/data").then(r=>r.json()).then(d=>{
        if(!d || typeof d !== 'object') return;
        history.push(d);if(history.length>MAX_HISTORY)history.shift();
        checkAlerts(d.top_procs);
        if(currentTab==='dashboard'){
            if(dashFirstPaint){renderDash(d);dashFirstPaint=false;}
            else patchDash(d);
        }
        if(d.charging && !prevCharging)fetch('/charge/start?level='+d.bat_level,{method:'POST'}).catch(()=>{});
        prevCharging=d.charging;
        if(currentTab==='charge'){fetch('/charge/state').then(r=>r.json()).then(cs=>{chargePoints=cs.points||[];renderChargeChart();}).catch(()=>{});}
        if(!d.screen_on && prevScreenOn)fetch('/powersave/trigger',{method:'POST'}).catch(()=>{});
        prevScreenOn=d.screen_on;
        if(currentTab==='dashboard'&&history.length>0){
            drawChart(history,'chart_temp','temp','#FF9800','温度',50);
            drawChart(history,'chart_discharge','discharge_ma','#FF9800','放电',Math.max(500,...history.map(d=>d.discharge_ma||0)));
        }
    }).catch(()=>{failCount++;if(failCount>=3)document.getElementById("dash").innerHTML="ADB连接失败，请检查手机连接";});
}
load();setInterval(load,__INTERVAL__);
</script>
</body>
</html>"""

HTML = HTML_TPL.replace("__REFRESH__", str(REFRESH_INTERVAL)).replace("__INTERVAL__", str(REFRESH_INTERVAL * 1000))


# ======================= HTTP Handler =======================

class SSEWriter:
    """SSE 流式输出辅助"""
    def __init__(self, wfile):
        self.wfile = wfile
    def send(self, data):
        self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()
    def heartbeat(self):
        self.wfile.write(": hb\n\n".encode())
        self.wfile.flush()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _json(self, data):
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("Connection","keep-alive")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        return SSEWriter(self.wfile)

    def _wrap(self, method):
        try: method()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError): pass

    def do_GET(self):
        self._wrap(self._do_GET)

    def _do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/core":
            self._json(get_core_data())

        elif parsed.path == "/data":
            data = get_data()
            update_mem_history(data.get("top_mem_procs",[]))
            update_battery_health()
            self._json(data)

        elif parsed.path == "/kill":
            pkg = qs.get("pkg",[""])[0]
            if pkg and not pkg.startswith("com.android") and not pkg.startswith("system"):
                try:
                    subprocess.run([ADB,"shell","am","force-stop",pkg],capture_output=True,timeout=5)
                    self._json({"ok":True,"pkg":pkg})
                except Exception as e: self._json({"ok":False,"error":str(e)})
            else: self._json({"ok":False,"error":"不允许强杀系统进程"})

        elif parsed.path == "/charge/state": self._json(get_charge_state())
        elif parsed.path == "/wakeup": self._json(get_wakeup_sources())
        elif parsed.path == "/leaks": self._json(check_mem_leaks())
        elif parsed.path == "/battery_health": self._json(predict_battery_life())
        elif parsed.path == "/powersave":
            with power_save_lock: self._json({"enabled":power_save_enabled,"whitelist":sorted(list(power_save_whitelist)),"kill_log":list(power_save_kill_log)})

        elif parsed.path == "/startup/history":
            with startup_lock: self._json(list(startup_history))

        elif parsed.path == "/gfxinfo":
            pkg = qs.get("pkg",[""])[0]
            self._json(get_gfxinfo(pkg) if pkg else {"error":"请提供包名"})

        elif parsed.path == "/traffic":
            self._json(get_net_traffic())

        elif parsed.path == "/monkey/history":
            with monkey_results_lock: self._json(list(monkey_results))

        elif parsed.path.startswith("/screenshot/"):
            shot_id = parsed.path.split("/")[-1]
            with screenshot_lock: path = screenshot_store.get(shot_id)
            if path and os.path.exists(path):
                self.send_response(200)
                self.send_header("Content-Type","image/png")
                self.end_headers()
                with open(path,"rb") as f: self.wfile.write(f.read())
            else:
                self.send_response(404); self.end_headers()

        # SSE 端点
        elif parsed.path == "/logcat/stream":
            pkg = qs.get("pkg",[""])[0]
            stream_id = start_logcat_stream(pkg)
            if not stream_id:
                self._json({"error":"无法启动logcat"})
                return
            sse = self._sse_headers()
            try:
                with logcat_lock: info = logcat_streams.get(stream_id)
                if not info: return
                q = info["queue"]; stop = info["stop"]
                while not stop.is_set():
                    try:
                        line = q.get(timeout=1)
                        sse.send(line)
                    except queue.Empty:
                        sse.heartbeat()
                sse.send({"done":True})
            except: pass

        elif parsed.path == "/monkey/stream":
            pkg = qs.get("pkg",[""])[0]
            count = int(qs.get("count",["500"])[0])
            throttle = int(qs.get("throttle",["200"])[0])
            stream_id = start_monkey(pkg, count, throttle)
            if not stream_id:
                self._json({"error":"无法启动Monkey"})
                return
            sse = self._sse_headers()
            try:
                with monkey_lock: info = monkey_streams.get(stream_id)
                if not info: return
                q = info["queue"]; stop = info["stop"]
                while not stop.is_set():
                    try:
                        line = q.get(timeout=1)
                        if isinstance(line, dict) and line.get("done"):
                            # 自然完成：直接推送 done 并退出
                            sse.send({"done":True,"events":line.get("events",0),"total":count,"crashes":line.get("crashes",0)})
                            return
                        sse.send(line)
                    except queue.Empty:
                        sse.heartbeat()
                # 手动停止
                with monkey_results_lock:
                    recent = list(monkey_results)
                last = recent[-1] if recent else {"events":0,"crashes":0,"anrs":0}
                sse.send({"done":True,"events":last.get("events",0),"total":count,"crashes":last.get("crashes",0)})
            except: pass

        else:
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(HTML.encode())

    def do_POST(self):
        self._wrap(self._do_POST)

    def _do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/charge/start":
            level = int(qs.get("level",["0"])[0])
            start_charge_session(level); self._json({"ok":True})

        elif parsed.path == "/powersave/toggle":
            global power_save_enabled
            with power_save_lock: power_save_enabled = not power_save_enabled
            save_power_save_state(); self._json({"enabled":power_save_enabled})

        elif parsed.path == "/powersave/trigger":
            data = get_data()
            if not data.get("screen_on"): handle_screen_off_power_save(False, data.get("top_procs",[]))
            self._json({"ok":True})

        elif parsed.path == "/powersave/whitelist/add":
            pkg = qs.get("pkg",[""])[0]
            if pkg:
                with power_save_lock: power_save_whitelist.add(pkg)
                save_power_save_state()
            self._json({"ok":True})

        elif parsed.path == "/powersave/whitelist/remove":
            pkg = qs.get("pkg",[""])[0]
            with power_save_lock: power_save_whitelist.discard(pkg)
            save_power_save_state(); self._json({"ok":True})

        elif parsed.path == "/restart":
            self._json({"ok":True,"message":"正在重启服务..."})
            threading.Thread(target=do_restart, daemon=True).start()

        # v6 端点
        elif parsed.path == "/startup":
            pkg = qs.get("pkg",[""])[0]
            self._json(get_app_startup(pkg) if pkg else {"status":"请提供包名"})

        elif parsed.path == "/logcat/stop":
            stop_logcat_stream("__all__")  # 停止所有
            # 停止所有活跃流
            with logcat_lock:
                for sid in list(logcat_streams.keys()):
                    s = logcat_streams[sid]; s["stop"].set()
                logcat_streams.clear()
            self._json({"ok":True})

        elif parsed.path == "/monkey/stop":
            with monkey_lock:
                for sid in list(monkey_streams.keys()):
                    s = monkey_streams[sid]; s["stop"].set()
                monkey_streams.clear()
            self._json({"ok":True})

        elif parsed.path == "/screenshot/take":
            self._json(take_screenshot())

        else:
            self._json({"ok":False,"error":"unknown endpoint"})


# ======================= Main =======================

def main():
    print("启动手机监控仪表盘 v6 ...")
    load_persisted_data()
    ct = threading.Thread(target=charge_sample_thread, daemon=True); ct.start()

    server = None
    for attempt in range(10):
        try:
            server = http.server.HTTPServer(("127.0.0.1", PORT), Handler); break
        except OSError:
            print(f"端口 {PORT} 被占用，重试 {attempt+1}/10..."); time.sleep(1)
    if server is None: print(f"无法绑定端口 {PORT}"); return

    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    print(f"仪表盘已打开: http://127.0.0.1:{PORT}")
    print("按 Ctrl+C 退出")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: print("\n已退出")


if __name__ == "__main__":
    main()
