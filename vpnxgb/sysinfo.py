"""Estado del servidor (CPU, RAM, disco, tiempo encendido) leído de /proc."""

import os
import shutil


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def server_info() -> dict:
    load = _read("/proc/loadavg").split()
    cpus = os.cpu_count() or 1
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts:
            mem[key] = int(parts[0]) * 1024
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    uptime = _read("/proc/uptime").split()
    try:
        disk = shutil.disk_usage("/")
        disk_used, disk_total = disk.used, disk.total
    except OSError:
        disk_used = disk_total = 0
    load1 = float(load[0]) if load else 0.0
    return {
        "cpus": cpus,
        "load1": load1,
        "cpu_pct": min(round(100 * load1 / cpus), 100),
        "mem_total": total,
        "mem_used": total - available,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "uptime": int(float(uptime[0])) if uptime else 0,
    }
