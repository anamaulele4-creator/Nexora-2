"""Diagnóstico do servidor: CPU, RAM, disco e GPU NVIDIA (se existir)."""

from __future__ import annotations

import os
import shutil
import subprocess


def _meminfo_gb() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        return None
    return None


def gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            result.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                           "vram_used_gb": round(float(parts[2]) / 1024, 1), "driver": parts[3]})
    return result


def hardware(models_path: str = "/") -> dict:
    disk = shutil.disk_usage(models_path if os.path.exists(models_path) else "/")
    g = gpus()
    return {
        "cpus": os.cpu_count(),
        "ram_gb": _meminfo_gb(),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "gpus": g,
        "vram_gb": max((x["vram_gb"] for x in g), default=None),
    }
