"""In-memory backend monitoring and metrics collection for SAIR.

Tracks system metrics, request counts, error rates, latencies, resource utilisation
(CPU, RAM, Disk, Python runtime, threads), and recent requests strictly in-memory.
Zero data is written to or queried from MongoDB collections — completely ephemeral and
invisible on the main database.
"""

from __future__ import annotations

import ctypes
import gc
import os
import platform
import re
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# CPU & Resource Trackers (Cross-platform: Windows, Linux, macOS)
# ─────────────────────────────────────────────────────────────────────────────

class _WindowsResourceTracker:
    """Uses Win32 APIs via ctypes for sub-millisecond, non-blocking metrics."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._psapi = ctypes.windll.psapi
        self._kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        self._h_proc = self._kernel32.GetCurrentProcess()

        # Define argtypes for safety on 64-bit Windows
        self._kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]

        # System CPU tracking state
        self._last_sys_time = time.time()
        self._last_idle_ticks, self._last_total_ticks = self._get_system_ticks()
        self._last_sys_cpu_pct = 0.0

        # Process CPU tracking state
        self._last_proc_time = time.time()
        self._last_proc_ticks = self._get_process_ticks()
        self._last_proc_cpu_pct = 0.0

        self._cores = os.cpu_count() or 1

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_uint32),
            ("dwHighDateTime", ctypes.c_uint32),
        ]

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    def _ft_to_int(self, ft: _FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def _get_system_ticks(self) -> tuple[int, int]:
        try:
            idle, kernel, user = self._FILETIME(), self._FILETIME(), self._FILETIME()
            self._kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            i = self._ft_to_int(idle)
            k = self._ft_to_int(kernel)
            u = self._ft_to_int(user)
            return i, (k + u)
        except Exception:
            return 0, 0

    def _get_process_ticks(self) -> int:
        try:
            c, e, k, u = self._FILETIME(), self._FILETIME(), self._FILETIME(), self._FILETIME()
            self._kernel32.GetProcessTimes(
                self._h_proc,
                ctypes.byref(c),
                ctypes.byref(e),
                ctypes.byref(k),
                ctypes.byref(u),
            )
            return self._ft_to_int(k) + self._ft_to_int(u)
        except Exception:
            return 0

    def get_system_cpu_percent(self) -> float:
        now = time.time()
        elapsed = now - self._last_sys_time
        if elapsed < 0.2:  # Cache brief reads to avoid 0-delta noise
            return self._last_sys_cpu_pct

        idle, total = self._get_system_ticks()
        delta_total = total - self._last_total_ticks
        delta_idle = idle - self._last_idle_ticks

        if delta_total > 0:
            pct = max(0.0, min(100.0, round((delta_total - delta_idle) / delta_total * 100.0, 1)))
            self._last_sys_cpu_pct = pct

        self._last_idle_ticks = idle
        self._last_total_ticks = total
        self._last_sys_time = now
        return self._last_sys_cpu_pct

    def get_process_cpu_percent(self) -> float:
        now = time.time()
        elapsed = now - self._last_proc_time
        if elapsed < 0.2:
            return self._last_proc_cpu_pct

        ticks = self._get_process_ticks()
        delta_proc_ticks = ticks - self._last_proc_ticks
        proc_seconds = delta_proc_ticks * 1e-7

        if elapsed > 0:
            pct = max(0.0, min(100.0, round((proc_seconds / (elapsed * self._cores)) * 100.0, 1)))
            self._last_proc_cpu_pct = pct

        self._last_proc_ticks = ticks
        self._last_proc_time = now
        return self._last_proc_cpu_pct

    def get_memory_info(self) -> tuple[dict[str, Any], dict[str, Any]]:
        sys_mem: dict[str, Any] = {
            "total_mb": 0.0,
            "avail_mb": 0.0,
            "used_mb": 0.0,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "avail_gb": 0.0,
            "percent": 0.0,
        }
        proc_mem: dict[str, Any] = {
            "rss_mb": 0.0,
            "peak_rss_mb": 0.0,
            "private_mb": 0.0,
        }

        try:
            stat = self._MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(self._MEMORYSTATUSEX)
            if self._kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_mb = stat.ullTotalPhys / (1024 * 1024)
                avail_mb = stat.ullAvailPhys / (1024 * 1024)
                used_mb = total_mb - avail_mb
                sys_mem["total_mb"] = round(total_mb, 1)
                sys_mem["avail_mb"] = round(avail_mb, 1)
                sys_mem["used_mb"] = round(used_mb, 1)
                sys_mem["total_gb"] = round(total_mb / 1024, 2)
                sys_mem["avail_gb"] = round(avail_mb / 1024, 2)
                sys_mem["used_gb"] = round(used_mb / 1024, 2)
                sys_mem["percent"] = float(stat.dwMemoryLoad)
        except Exception:
            pass

        try:
            pmc = self._PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(self._PROCESS_MEMORY_COUNTERS_EX)
            self._psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(self._PROCESS_MEMORY_COUNTERS_EX),
                ctypes.c_ulong,
            ]
            if self._psapi.GetProcessMemoryInfo(self._h_proc, ctypes.byref(pmc), pmc.cb):
                proc_mem["rss_mb"] = round(pmc.WorkingSetSize / (1024 * 1024), 2)
                proc_mem["peak_rss_mb"] = round(pmc.PeakWorkingSetSize / (1024 * 1024), 2)
                proc_mem["private_mb"] = round(pmc.PrivateUsage / (1024 * 1024), 2)
        except Exception:
            pass

        return sys_mem, proc_mem


class _GenericResourceTracker:
    """Accurate POSIX / Linux resource tracker using /proc filesystem metrics."""

    def __init__(self) -> None:
        self._cores = os.cpu_count() or 1
        self._last_time = time.time()
        self._last_total = 0.0
        self._last_idle = 0.0
        self._last_proc_time = time.time()
        self._last_proc_ticks = 0.0
        self._init_proc_stat()

    def _init_proc_stat(self) -> None:
        try:
            if os.path.exists("/proc/stat"):
                with open("/proc/stat", "r") as f:
                    fields = [float(x) for x in f.readline().strip().split()[1:]]
                self._last_idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                self._last_total = sum(fields)
        except Exception:
            pass

    def get_system_cpu_percent(self) -> float:
        try:
            if os.path.exists("/proc/stat"):
                with open("/proc/stat", "r") as f:
                    fields = [float(x) for x in f.readline().strip().split()[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                total = sum(fields)
                d_total = total - self._last_total
                d_idle = idle - self._last_idle
                self._last_total = total
                self._last_idle = idle
                if d_total > 0:
                    pct = round(((d_total - d_idle) / d_total) * 100.0, 1)
                    return max(0.0, min(100.0, pct))
            elif hasattr(os, "getloadavg"):
                load1, _, _ = os.getloadavg()
                return min(100.0, round((load1 / self._cores) * 100.0, 1))
        except Exception:
            pass
        return 0.0

    def get_process_cpu_percent(self) -> float:
        try:
            now = time.time()
            dt = now - self._last_proc_time
            if dt < 0.2:
                return 0.0
            if os.path.exists("/proc/self/stat"):
                with open("/proc/self/stat", "r") as f:
                    parts = f.read().split()
                # 13: utime, 14: stime
                ticks = float(parts[13]) + float(parts[14])
                clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
                d_ticks = (ticks - self._last_proc_ticks) / clk_tck
                self._last_proc_ticks = ticks
                self._last_proc_time = now
                if dt > 0 and self._cores > 0:
                    pct = round((d_ticks / dt / self._cores) * 100.0, 1)
                    return max(0.0, min(100.0, pct))
        except Exception:
            pass
        return 0.0

    def get_memory_info(self) -> tuple[dict[str, Any], dict[str, Any]]:
        sys_mem = {
            "total_mb": 0.0,
            "avail_mb": 0.0,
            "used_mb": 0.0,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "avail_gb": 0.0,
            "percent": 0.0,
        }
        proc_mem = {
            "rss_mb": 0.0,
            "peak_rss_mb": 0.0,
            "private_mb": 0.0,
        }

        try:
            if os.path.exists("/proc/meminfo"):
                mem = {}
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        parts = line.split(":")
                        if len(parts) == 2:
                            key = parts[0].strip()
                            val = parts[1].strip().split()[0]
                            mem[key] = int(val)
                total_kb = mem.get("MemTotal", 0)
                avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
                used_kb = total_kb - avail_kb
                if total_kb > 0:
                    sys_mem["total_mb"] = round(total_kb / 1024, 1)
                    sys_mem["avail_mb"] = round(avail_kb / 1024, 1)
                    sys_mem["used_mb"] = round(used_kb / 1024, 1)
                    sys_mem["total_gb"] = round(total_kb / (1024 * 1024), 2)
                    sys_mem["avail_gb"] = round(avail_kb / (1024 * 1024), 2)
                    sys_mem["used_gb"] = round(used_kb / (1024 * 1024), 2)
                    sys_mem["percent"] = round((used_kb / total_kb) * 100.0, 1)
        except Exception:
            pass

        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF)
            factor = 1024 if sys.platform != "darwin" else 1024 * 1024
            proc_mem["rss_mb"] = round(ru.ru_maxrss / factor, 2)
            proc_mem["peak_rss_mb"] = proc_mem["rss_mb"]
        except Exception:
            pass

        return sys_mem, proc_mem


# ─────────────────────────────────────────────────────────────────────────────
# In-Memory Metrics Store
# ─────────────────────────────────────────────────────────────────────────────

class MetricsCollector:
    """Thread-safe, pure in-memory metrics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.start_datetime = datetime.now(timezone.utc)

        # Core counters
        self.total_requests = 0
        self.active_requests = 0
        self.total_errors = 0  # 4xx + 5xx

        # Distributions
        self.methods: dict[str, int] = {}
        self.status_codes: dict[int, int] = {}
        self.status_classes: dict[str, int] = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}

        # Per-endpoint tracking: normalized_path -> stats
        self.endpoints: dict[str, dict[str, Any]] = {}

        # Latency statistics
        self.total_duration_ms = 0.0
        self.min_duration_ms = float("inf")
        self.max_duration_ms = 0.0
        self.recent_latencies: deque[float] = deque(maxlen=200)

        # Rate calculations (sliding 60-second window)
        self.request_timestamps: deque[float] = deque(maxlen=2000)

        # Rolling buffer of recent requests
        self.recent_requests: deque[dict[str, Any]] = deque(maxlen=50)
        self._request_seq = 0

        # Hardware & resource tracker
        if sys.platform == "win32":
            self._resource_tracker: Any = _WindowsResourceTracker()
        else:
            self._resource_tracker = _GenericResourceTracker()

    def record_in_flight_start(self) -> None:
        with self._lock:
            self.active_requests += 1

    def record_in_flight_end(self) -> None:
        with self._lock:
            self.active_requests = max(0, self.active_requests - 1)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Group dynamic URLs (e.g. participant IDs) to prevent high-cardinality maps."""
        norm = re.sub(r"/u-[0-9a-fA-F]{16}", "/{id}", path)
        norm = re.sub(r"/[0-9a-fA-F]{24}", "/{id}", norm)
        if norm.startswith("/admin/static/"):
            return "/admin/static/*"
        return norm

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        client_ip: str = "unknown",
    ) -> None:
        norm_path = self._normalize_path(path)
        # Exclude internal telemetry endpoints from distorting study metrics & request feeds
        if norm_path in ("/admin/mets", "/admin/metrics", "/admin/api/mets", "/health", "/mets") or norm_path.startswith("/admin/static/"):
            return

        now = time.time()
        status_class = f"{status_code // 100}xx" if 100 <= status_code < 600 else "other"
        is_error = status_code >= 400

        with self._lock:
            self._request_seq += 1
            seq = self._request_seq
            self.total_requests += 1
            self.request_timestamps.append(now)

            if is_error:
                self.total_errors += 1

            self.methods[method] = self.methods.get(method, 0) + 1
            self.status_codes[status_code] = self.status_codes.get(status_code, 0) + 1
            if status_class in self.status_classes:
                self.status_classes[status_class] += 1

            self.total_duration_ms += duration_ms
            self.recent_latencies.append(duration_ms)
            if duration_ms < self.min_duration_ms:
                self.min_duration_ms = duration_ms
            if duration_ms > self.max_duration_ms:
                self.max_duration_ms = duration_ms

            ep = self.endpoints.setdefault(
                norm_path,
                {
                    "path": norm_path,
                    "count": 0,
                    "total_ms": 0.0,
                    "min_ms": float("inf"),
                    "max_ms": 0.0,
                    "errors": 0,
                    "methods": {},
                },
            )
            ep["count"] += 1
            ep["total_ms"] += duration_ms
            if duration_ms < ep["min_ms"]:
                ep["min_ms"] = duration_ms
            if duration_ms > ep["max_ms"]:
                ep["max_ms"] = duration_ms
            if is_error:
                ep["errors"] += 1
            ep["methods"][method] = ep["methods"].get(method, 0) + 1

            dt = datetime.now(timezone.utc)
            self.recent_requests.appendleft({
                "id": seq,
                "timestamp": dt.isoformat(),
                "time_str": dt.strftime("%H:%M:%S"),
                "method": method,
                "path": path,
                "norm_path": norm_path,
                "status_code": status_code,
                "status_class": status_class,
                "duration_ms": round(duration_ms, 2),
                "client_ip": client_ip,
            })

    def get_uptime_seconds(self) -> int:
        return int(time.time() - self.start_time)

    def get_uptime_formatted(self) -> str:
        seconds = self.get_uptime_seconds()
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours}h {mins}m {secs}s"
        if hours > 0:
            return f"{hours}h {mins}m {secs}s"
        return f"{mins}m {secs}s"

    def get_total_requests(self) -> int:
        with self._lock:
            return self.total_requests

    def _calc_rates(self) -> tuple[float, int]:
        now = time.time()
        cutoff = now - 60.0
        while self.request_timestamps and self.request_timestamps[0] < cutoff:
            self.request_timestamps.popleft()
        reqs_in_last_minute = len(self.request_timestamps)
        reqs_per_sec = round(reqs_in_last_minute / 60.0, 2)
        return reqs_per_sec, reqs_in_last_minute

    def get_disk_info(self) -> dict[str, Any]:
        try:
            disk = shutil.disk_usage(".")
            total_gb = round(disk.total / (1024**3), 2)
            used_gb = round(disk.used / (1024**3), 2)
            free_gb = round(disk.free / (1024**3), 2)
            pct = round((disk.used / disk.total) * 100.0, 1) if disk.total > 0 else 0.0
            return {
                "path": os.path.abspath("."),
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "percent": pct,
            }
        except Exception:
            return {
                "path": ".",
                "total_gb": 0.0,
                "used_gb": 0.0,
                "free_gb": 0.0,
                "percent": 0.0,
            }

    def get_metrics_snapshot(self, db_health: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build a comprehensive point-in-time metrics snapshot dictionary. Completely in-memory and non-blocking."""
        with self._lock:
            total_reqs = self.total_requests
            active_reqs = self.active_requests
            total_errs = self.total_errors
            methods_copy = dict(self.methods)
            status_codes_copy = {str(k): v for k, v in sorted(self.status_codes.items())}
            status_classes_copy = dict(self.status_classes)

            avg_lat = round(self.total_duration_ms / total_reqs, 2) if total_reqs > 0 else 0.0
            min_lat = round(self.min_duration_ms, 2) if self.min_duration_ms != float("inf") else 0.0
            max_lat = round(self.max_duration_ms, 2)
            recent_avg_lat = (
                round(sum(self.recent_latencies) / len(self.recent_latencies), 2)
                if self.recent_latencies
                else 0.0
            )

            reqs_per_sec, reqs_per_min = self._calc_rates()
            success_rate = (
                round(((total_reqs - total_errs) / total_reqs) * 100.0, 1)
                if total_reqs > 0
                else 100.0
            )
            error_rate = (
                round((total_errs / total_reqs) * 100.0, 1)
                if total_reqs > 0
                else 0.0
            )

            rate_limit_429 = self.status_codes.get(429, 0)
            server_errors_5xx = sum(v for k, v in self.status_codes.items() if 500 <= k < 600)
            client_errors_4xx = sum(v for k, v in self.status_codes.items() if 400 <= k < 500)

            endpoints_list = []
            for path, ep in self.endpoints.items():
                c = ep["count"]
                endpoints_list.append({
                    "path": path,
                    "count": c,
                    "share_percent": round((c / total_reqs) * 100.0, 1) if total_reqs > 0 else 0.0,
                    "avg_ms": round(ep["total_ms"] / c, 2) if c > 0 else 0.0,
                    "min_ms": round(ep["min_ms"], 2) if ep["min_ms"] != float("inf") else 0.0,
                    "max_ms": round(ep["max_ms"], 2),
                    "errors": ep["errors"],
                    "methods": dict(ep["methods"]),
                })
            endpoints_list.sort(key=lambda x: x["count"], reverse=True)
            recent_list = list(self.recent_requests)

        # Resource measurements
        sys_cpu = self._resource_tracker.get_system_cpu_percent()
        proc_cpu = self._resource_tracker.get_process_cpu_percent()
        sys_mem, proc_mem = self._resource_tracker.get_memory_info()
        disk_info = self.get_disk_info()

        # Rate limiter in-memory queue metrics
        rate_limiter_stats = {"tracked_score_users": 0, "tracked_register_ips": 0}
        try:
            import rate_limit
            if hasattr(rate_limit, "_score_hits"):
                rate_limiter_stats["tracked_score_users"] = len(rate_limit._score_hits)
            if hasattr(rate_limit, "_register_hits"):
                rate_limiter_stats["tracked_register_ips"] = len(rate_limit._register_hits)
        except Exception:
            pass

        db_status = db_health or {
            "status": "connected (unqueried)",
            "connected": True,
            "ping_ms": None,
            "storage_mode": "in-memory-only (not saved on main db)",
        }

        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server": {
                "service": "sair-backend",
                "version": "0.1.0",
                "started_at": self.start_datetime.isoformat(),
                "uptime_seconds": self.get_uptime_seconds(),
                "uptime_formatted": self.get_uptime_formatted(),
                "pid": os.getpid(),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "os_name": platform.system(),
                "threads_active": threading.active_count(),
            },
            "traffic": {
                "total_requests": total_reqs,
                "active_requests": active_reqs,
                "total_errors": total_errs,
                "rate_limits_429": rate_limit_429,
                "server_errors_5xx": server_errors_5xx,
                "client_errors_4xx": client_errors_4xx,
                "requests_per_minute": reqs_per_min,
                "requests_per_second": reqs_per_sec,
                "success_rate_percent": success_rate,
                "error_rate_percent": error_rate,
                "status_classes": status_classes_copy,
                "status_codes": status_codes_copy,
                "methods": methods_copy,
            },
            "latency_ms": {
                "avg": recent_avg_lat if recent_avg_lat > 0 else avg_lat,
                "all_time_avg": avg_lat,
                "recent_avg_200": recent_avg_lat,
                "min": min_lat,
                "max": max_lat,
            },
            "resources": {
                "cpu": {
                    "system_percent": sys_cpu,
                    "process_percent": proc_cpu,
                    "cores_logical": os.cpu_count() or 1,
                },
                "memory": {
                    "process_rss_mb": proc_mem["rss_mb"],
                    "process_peak_rss_mb": proc_mem["peak_rss_mb"],
                    "process_private_mb": proc_mem["private_mb"],
                    "system_total_gb": sys_mem["total_gb"],
                    "system_used_gb": sys_mem["used_gb"],
                    "system_avail_gb": sys_mem["avail_gb"],
                    "system_percent": sys_mem["percent"],
                },
                "disk": disk_info,
            },
            "database": db_status,
            "rate_limiter": rate_limiter_stats,
            "endpoints": endpoints_list,
            "recent_requests": recent_list,
        }


# Global singleton instance
collector = MetricsCollector()

# Public helper functions
def record_in_flight_start() -> None:
    collector.record_in_flight_start()

def record_in_flight_end() -> None:
    collector.record_in_flight_end()

def record_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_ip: str = "unknown",
) -> None:
    collector.record_request(method, path, status_code, duration_ms, client_ip)

def get_uptime_formatted() -> str:
    return collector.get_uptime_formatted()

def get_total_requests() -> int:
    return collector.get_total_requests()

def get_metrics_snapshot(db_health: dict[str, Any] | None = None) -> dict[str, Any]:
    return collector.get_metrics_snapshot(db_health=db_health)