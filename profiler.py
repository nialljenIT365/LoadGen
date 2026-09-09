#!/usr/bin/env python3
"""Profiler: measures one Reference Session and writes a User Profile.

A real person logs into the session host, starts this script, works through
CHECKLIST.md, then stops it. The Profiler samples the cost of its own Windows
session -- every process sharing its session ID -- and writes a User Profile
that loadgen.py can load with `--profile`.

It measures its own session from the inside, as a standard user, rather than
subtracting a Baseline from whole-host figures. That needs no admin rights and
no separate Baseline capture, at the cost of missing SYSTEM-owned per-session
processes. See docs/adr/0002-profiler-measures-own-session.md.

Vocabulary used throughout this file is defined in CONTEXT.md.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import signal
import sys
import time
from datetime import datetime

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TICK_SECONDS = 2.0
WARMUP_SECONDS = 60.0

# Below this many retained samples the run is too short to trust: the file is
# still written, but meta.too_short says so.
MIN_SAMPLES = 30

CHECKLIST_NAME = "CHECKLIST.md"

# The four User Profile keys, in the order loadgen.py reads them.
PROFILE_KEYS = ("gpu", "cpu", "vram_mb", "ram_gb")

# Metric key -> the sample field it is computed from.
METRICS = ("gpu", "cpu", "vram_mb", "ram_gb")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_duration(text: str) -> float:
    """'90s', '30m', '2h' -> seconds. A bare number is seconds."""
    text = text.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("empty duration")
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    factor = 1.0
    if text[-1] in units:
        factor = units[text[-1]]
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad duration {text!r}; use 90s, 30m or 2h"
        ) from None
    if value <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return value * factor


def warn(message: str) -> None:
    print(f"warning: {message}", flush=True)


def mean(values) -> float:
    return sum(values) / len(values) if values else 0.0


def p95(values) -> float:
    """Nearest-rank 95th percentile: the smallest sample at or above 95%.

    No interpolation, so the figure is always a value that was actually
    observed -- which is what an operator sizing a host wants to see.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = -(-len(ordered) * 95 // 100) - 1        # ceil(n * 0.95) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarise(values) -> dict:
    return {
        "mean": round(mean(values), 1),
        "p95": round(p95(values), 1),
        "peak": round(max(values), 1) if values else 0.0,
    }


def fmt(value, digits: int = 1) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------------------
# The session: which processes belong to this Reference Session
# --------------------------------------------------------------------------

class SessionSet:
    """The processes sharing this script's Windows session ID.

    Re-enumerated every tick because apps start and stop during a Reference
    Session. Processes that refuse to answer are skipped and counted once each
    in `skipped`, which the output meta reports so the under-count is visible.
    """

    def __init__(self, psutil_module) -> None:
        self._psutil = psutil_module
        self._own_pid = os.getpid()
        self._kernel32 = None
        self.session_id = None
        # pid -> (create_time, session_id); create_time guards against a pid
        # being reused by a different process during a long run.
        self._sessions: dict[int, tuple] = {}
        # Live psutil.Process objects, kept across ticks so cpu_percent() has a
        # previous call to measure the interval against.
        self._procs: dict[int, object] = {}
        self._skipped: set = set()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            self._kernel32 = ctypes.WinDLL("kernel32.dll")
            self._kernel32.ProcessIdToSessionId.restype = ctypes.c_int
            self._kernel32.ProcessIdToSessionId.argtypes = [
                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        except Exception:
            return False
        self.session_id = self._session_of(self._own_pid)
        return self.session_id is not None

    def _session_of(self, pid: int):
        out = ctypes.c_ulong(0)
        try:
            if self._kernel32.ProcessIdToSessionId(pid, ctypes.byref(out)) == 0:
                return None
        except Exception:
            return None
        return int(out.value)

    @property
    def skipped(self) -> int:
        return len(self._skipped)

    def sample(self):
        """(pids, cpu_percent_of_host, ram_gb) for this tick."""
        psutil = self._psutil
        pids = []
        cpu_total = 0.0
        rss_total = 0
        seen = set()
        visited = set()

        for proc in psutil.process_iter(["pid", "create_time"]):
            pid = proc.info["pid"]
            if pid == self._own_pid or pid == 0:
                continue
            visited.add(pid)
            created = proc.info["create_time"]
            cached = self._sessions.get(pid)
            if cached is None or cached[0] != created:
                session = self._session_of(pid)
                if session is None:
                    self._skipped.add((pid, created))
                    continue
                self._sessions[pid] = (created, session)
                cached = self._sessions[pid]
            if cached[1] != self.session_id:
                continue

            seen.add(pid)
            live = self._procs.get(pid)
            if live is None:
                live = proc
                self._procs[pid] = live
                # First call always reads 0.0; it seeds the interval so the
                # next tick is meaningful.
                try:
                    live.cpu_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self._skipped.add((pid, created))
                    continue
            try:
                cpu_total += live.cpu_percent()
                rss_total += live.memory_info().rss
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                self._skipped.add((pid, created))
                continue
            pids.append(pid)

        # Drop cache entries for processes that have exited. Session IDs are
        # kept for every process on the host, not only this session's, so the
        # kernel32 call is made once per process rather than once per tick.
        for pid in set(self._procs) - seen:
            self._procs.pop(pid, None)
        for pid in set(self._sessions) - visited:
            self._sessions.pop(pid, None)

        cores = os.cpu_count() or 1
        return pids, cpu_total / cores, rss_total / 2 ** 30


def loadgen_is_running(psutil_module) -> bool:
    """The host is meant to be idle; a running loadgen.py would poison the run."""
    for proc in psutil_module.process_iter(["pid", "cmdline"]):
        if proc.info["pid"] == os.getpid():
            continue
        try:
            parts = proc.info["cmdline"] or []
        except Exception:
            continue
        if any("loadgen.py" in str(part) for part in parts):
            return True
    return False


# --------------------------------------------------------------------------
# NVML: per-process GPU and VRAM, plus the whole-host cross-check
# --------------------------------------------------------------------------

class Nvml:
    """NVML handle for the vGPU, used per process where the driver allows it."""

    def __init__(self) -> None:
        self.ok = False
        self.error = ""
        self.hint = ""
        self.proc_util_ok = True    # cleared if the driver says NotSupported
        self.proc_mem_ok = True
        self._lib = None
        self._handle = None
        self._last_seen = 0

    def start(self) -> bool:
        try:
            import pynvml
        except ImportError as exc:
            self.error = f"nvidia-ml-py is not installed ({exc})"
            self.hint = "pip install -r requirements.txt"
            return False
        try:
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception as exc:
            self.error = f"NVML could not open the GPU ({exc})"
            self.hint = "check that the NVIDIA GRID guest driver is installed"
            return False
        self._lib = pynvml
        self._last_seen = int(time.time() * 1_000_000) - int(TICK_SECONDS * 1_000_000)
        self.ok = True
        return True

    def host_utilization(self):
        if not self.ok:
            return None
        try:
            return float(self._lib.nvmlDeviceGetUtilizationRates(self._handle).gpu)
        except Exception:
            return None

    def process_utilization(self, pids):
        """Sum of smUtil across the session's processes, percent of the vGPU.

        Returns None when the driver does not support per-process utilization,
        which is the signal to fall back to the GPU Engine counter.
        """
        if not self.ok or not self.proc_util_ok:
            return None
        wanted = set(pids)
        try:
            samples = self._lib.nvmlDeviceGetProcessUtilization(
                self._handle, self._last_seen)
        except Exception as exc:
            if type(exc).__name__ == "NVMLError_NotSupported":
                self.proc_util_ok = False
                return None
            # NotFound simply means nothing was busy in the window.
            return 0.0
        newest: dict[int, tuple] = {}
        for sample in samples or []:
            pid = int(sample.pid)
            if pid not in wanted:
                continue
            stamp = int(sample.timeStamp)
            if pid not in newest or stamp > newest[pid][0]:
                newest[pid] = (stamp, float(sample.smUtil))
        if samples:
            self._last_seen = max(
                self._last_seen, max(int(s.timeStamp) for s in samples))
        return sum(value for _, value in newest.values())

    def process_memory(self, pids):
        """Sum of usedGpuMemory across the session's processes, in MB."""
        if not self.ok or not self.proc_mem_ok:
            return None
        wanted = set(pids)
        total = 0
        found = False
        for getter in ("nvmlDeviceGetGraphicsRunningProcesses",
                       "nvmlDeviceGetComputeRunningProcesses"):
            try:
                entries = getattr(self._lib, getter)(self._handle)
            except Exception as exc:
                if type(exc).__name__ == "NVMLError_NotSupported":
                    self.proc_mem_ok = False
                    return None
                continue
            found = True
            for entry in entries or []:
                if int(entry.pid) not in wanted:
                    continue
                used = getattr(entry, "usedGpuMemory", None)
                if used is None:
                    continue
                total += int(used)
        if not found:
            return None
        return total / 2 ** 20

    def shutdown(self) -> None:
        if self.ok:
            try:
                self._lib.nvmlShutdown()
            except Exception:
                pass
            self.ok = False


# --------------------------------------------------------------------------
# Windows performance counters, read through PDH
# --------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_LARGE = 0x00000400
PDH_MORE_DATA = 0x800007D2


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong),
                ("doubleValue", ctypes.c_double)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _CounterValue)]


class _LargeValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong),
                ("largeValue", ctypes.c_longlong)]


class _LargeItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _LargeValue)]


class PdhCounter:
    """One multi-instance performance counter, sampled on demand.

    The fallback path when NVML will not attribute GPU work per process, and
    the source of the whole-host GPU Engine total used for the cross-check.
    Any failure yields None and the console prints `--`.
    """

    def __init__(self, path: str, large: bool = False) -> None:
        self.path = path
        self.ok = False
        self._large = large
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            pdh = ctypes.WinDLL("pdh.dll")
            # PDH returns unsigned status codes and takes pointer-width
            # handles; both need declaring or 64-bit values are truncated.
            handle = ctypes.c_void_p
            pdh.PdhOpenQueryW.restype = ctypes.c_ulong
            pdh.PdhOpenQueryW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(handle)]
            pdh.PdhAddEnglishCounterW.restype = ctypes.c_ulong
            pdh.PdhAddEnglishCounterW.argtypes = [
                handle, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(handle)]
            pdh.PdhCollectQueryData.restype = ctypes.c_ulong
            pdh.PdhCollectQueryData.argtypes = [handle]
            pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong
            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                handle, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
            if pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)) != 0:
                return False
            if pdh.PdhAddEnglishCounterW(
                self._query, self.path, None, ctypes.byref(self._counter)
            ) != 0:
                return False
            # A rate counter needs one prior collection before it reads.
            pdh.PdhCollectQueryData(self._query)
        except Exception:
            return False
        self._pdh = pdh
        self.ok = True
        return True

    def sample(self):
        """{instance name: value} or None."""
        if not self.ok:
            return None
        size = ctypes.c_ulong(0)
        count = ctypes.c_ulong(0)
        fmt_flag = PDH_FMT_LARGE if self._large else PDH_FMT_DOUBLE
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            status = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, fmt_flag,
                ctypes.byref(size), ctypes.byref(count), None,
            )
            if status != PDH_MORE_DATA or count.value == 0:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            status = self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, fmt_flag,
                ctypes.byref(size), ctypes.byref(count), buffer,
            )
            if status != 0:
                return None
            item_type = _LargeItem if self._large else _CounterItem
            items = ctypes.cast(
                buffer, ctypes.POINTER(item_type * count.value)
            ).contents
        except Exception:
            return None
        out = {}
        for item in items:
            value = (item.FmtValue.largeValue if self._large
                     else item.FmtValue.doubleValue)
            out[item.szName or ""] = float(value)
        return out

    def close(self) -> None:
        self.ok = False


def sum_for_pids(instances, pids) -> float:
    """Sum every counter instance belonging to one of `pids`.

    GPU instance names look like
    `pid_1234_luid_0x00000000_0x0000C4C7_phys_0_eng_0_engtype_3D`. The trailing
    underscore in the prefix keeps pid_123 from matching pid_1234.
    """
    prefixes = tuple(f"pid_{pid}_" for pid in pids)
    if not prefixes:
        return 0.0
    return sum(value for name, value in instances.items()
               if name.startswith(prefixes))


# --------------------------------------------------------------------------
# Checklist header
# --------------------------------------------------------------------------

def read_checklist_header(path: str):
    """'name: office-generic' + 'version: 1' -> 'office-generic v1'."""
    name = version = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.lower().startswith("name:"):
                    name = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("version:"):
                    version = stripped.split(":", 1)[1].strip()
                if name and version:
                    break
    except OSError:
        return None
    if not name:
        return None
    return f"{name} v{version}" if version else name


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

CSV_COLUMNS = ("timestamp", "procs", "cpu", "ram_gb", "gpu", "vram_mb",
               "host_gpu_nvml", "host_gpu_engine")


def default_out_dir() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Documents", "LoadGen")


def write_outputs(out_dir: str, stem: str, samples, doc: dict):
    """Write <stem>.json and <stem>.csv. Returns both paths."""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, stem + ".json")
    csv_path = os.path.join(out_dir, stem + ".csv")

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for sample in samples:
            writer.writerow([
                sample["timestamp"],
                sample["procs"],
                f"{sample['cpu']:.1f}",
                f"{sample['ram_gb']:.2f}",
                "" if sample["gpu"] is None else f"{sample['gpu']:.1f}",
                "" if sample["vram_mb"] is None else f"{sample['vram_mb']:.1f}",
                "" if sample["host_gpu_nvml"] is None
                else f"{sample['host_gpu_nvml']:.1f}",
                "" if sample["host_gpu_engine"] is None
                else f"{sample['host_gpu_engine']:.1f}",
            ])

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")

    return json_path, csv_path


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="profiler.py",
        description=(
            "Measure one Reference Session and write a User Profile that "
            "loadgen.py can load with --profile."
        ),
    )
    parser.add_argument("--duration", type=parse_duration,
                        help="run time, e.g. 20m; default is until Ctrl+C")
    parser.add_argument("--warmup", type=float, default=WARMUP_SECONDS,
                        metavar="SECONDS",
                        help="seconds of samples to discard at the start "
                             f"(default {WARMUP_SECONDS:.0f}, 0 allowed)")
    parser.add_argument("--checklist", metavar="NAME",
                        help=f"checklist name recorded in the output; default "
                             f"is read from {CHECKLIST_NAME} beside this script")
    parser.add_argument("--out", metavar="DIR",
                        help="output folder; default "
                             "%%USERPROFILE%%\\Documents\\LoadGen")
    parser.add_argument("--tick", type=float, default=TICK_SECONDS,
                        metavar="SECONDS", help="seconds between samples")
    args = parser.parse_args(argv)

    if args.warmup < 0:
        print("error: --warmup cannot be negative", file=sys.stderr)
        return 2
    if args.tick <= 0:
        print("error: --tick must be positive", file=sys.stderr)
        return 2

    try:
        import psutil
    except ImportError:
        print("error: psutil is required. pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    here = os.path.dirname(os.path.abspath(__file__))

    session = SessionSet(psutil)
    if not session.start():
        print("error: cannot read this process's Windows session ID.",
              file=sys.stderr)
        print("       the Profiler measures its own session and only runs on "
              "Windows.", file=sys.stderr)
        return 2

    if loadgen_is_running(psutil):
        warn("loadgen.py appears to be running; its load will be measured as "
             "part of this session")

    # --- GPU stack. Absent is normal off the host; the columns go null. ---
    nvml = Nvml()
    gpu_available = nvml.start()
    gpu_reason = "" if gpu_available else nvml.error
    engine_counter = PdhCounter(r"\GPU Engine(*)\Utilization Percentage")
    process_memory_counter = PdhCounter(
        r"\GPU Process Memory(*)\Dedicated Usage", large=True)
    if gpu_available:
        engine_counter.start()
        process_memory_counter.start()
    else:
        warn(f"{nvml.error}; gpu and vram will be recorded as null")

    checklist = args.checklist or read_checklist_header(
        os.path.join(here, CHECKLIST_NAME))
    out_dir = args.out or default_out_dir()

    user = os.environ.get("USERNAME") or "user"
    host = os.environ.get("COMPUTERNAME") or "host"
    started_wall = datetime.now().astimezone()
    stem = f"profile-{user}-{started_wall.strftime('%Y-%m-%d-%H%M')}"

    if hasattr(signal, "SIGBREAK"):
        signal.signal(
            signal.SIGBREAK,
            lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    print(f"session {session.session_id} as {user} on {host}", flush=True)
    print(f"checklist: {checklist or 'not recorded'}", flush=True)
    print(f"tick {args.tick:.0f}s, warm-up {args.warmup:.0f}s discarded, "
          f"{'until Ctrl+C' if args.duration is None else f'{args.duration:.0f}s'}",
          flush=True)
    print(f"output folder: {out_dir}", flush=True)
    print("", flush=True)

    samples = []                    # retained samples only
    total_ticks = 0
    ratios = []
    method_gpu = None
    method_vram = None

    started_at = time.monotonic()
    deadline = None if args.duration is None else started_at + args.duration

    try:
        while True:
            tick_start = time.monotonic()
            elapsed = tick_start - started_at

            pids, cpu, ram_gb = session.sample()

            gpu_nvml = gpu_engine = None
            vram_nvml = vram_engine = None
            host_nvml = host_engine = None
            if gpu_available:
                host_nvml = nvml.host_utilization()
                instances = engine_counter.sample()
                if instances is not None:
                    host_engine = sum(instances.values())
                    gpu_engine = sum_for_pids(instances, pids)
                gpu_nvml = nvml.process_utilization(pids)
                vram_nvml = nvml.process_memory(pids)
                memory_instances = process_memory_counter.sample()
                if memory_instances is not None:
                    vram_engine = sum_for_pids(memory_instances, pids) / 2 ** 20
                if host_nvml is not None and host_engine:
                    ratios.append(host_nvml / host_engine)

                # Decide each method once, on the first tick with activity, and
                # never flip: a run whose figures came from two sources would
                # not be comparable with itself.
                if method_gpu is None:
                    if gpu_nvml:
                        method_gpu = "nvml"
                    elif gpu_engine:
                        method_gpu = "engine"
                if method_vram is None:
                    if vram_nvml:
                        method_vram = "nvml"
                    elif vram_engine:
                        method_vram = "engine"

            # Both sources are recorded every tick; the decided method selects
            # which series is reported, so no sample is sourced differently
            # from its neighbours.
            gpu = gpu_engine if method_gpu == "engine" else gpu_nvml
            vram = vram_engine if method_vram == "engine" else vram_nvml

            total_ticks += 1
            stamp = datetime.now()
            if elapsed >= args.warmup:
                samples.append({
                    "timestamp": stamp.isoformat(timespec="seconds"),
                    "procs": len(pids),
                    "cpu": cpu,
                    "ram_gb": ram_gb,
                    "gpu": gpu,
                    "vram_mb": vram,
                    "host_gpu_nvml": host_nvml,
                    "host_gpu_engine": host_engine,
                })

            marker = "" if elapsed >= args.warmup else "  (warm-up)"
            print(
                f"{stamp.strftime('%H:%M:%S')}  procs {len(pids):3d} | "
                f"cpu {fmt(cpu)} | ram {fmt(ram_gb)} GB | "
                f"gpu {fmt(gpu)} | vram {fmt(vram, 0)} MB{marker}",
                flush=True,
            )

            if deadline is not None and time.monotonic() >= deadline:
                print(f"\nduration reached after "
                      f"{time.monotonic() - started_at:.0f}s", flush=True)
                break

            rest = args.tick - (time.monotonic() - tick_start)
            if deadline is not None:
                rest = min(rest, max(0.0, deadline - time.monotonic()))
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        engine_counter.close()
        process_memory_counter.close()
        nvml.shutdown()

    duration_s = time.monotonic() - started_at

    if not samples:
        print(f"no samples retained after the {args.warmup:.0f}s warm-up; "
              "nothing written", flush=True)
        return 1

    # --- Statistics ---
    series = {key: [s[key] for s in samples if s[key] is not None]
              for key in METRICS}
    stats = {}
    for key in METRICS:
        stats[key] = summarise(series[key]) if series[key] else None

    per_user = {
        "gpu": round(mean(series["gpu"]), 1) if series["gpu"] else None,
        "cpu": round(mean(series["cpu"]), 1),
        "vram_mb": round(p95(series["vram_mb"]), 1) if series["vram_mb"] else None,
        "ram_gb": round(p95(series["ram_gb"]), 1),
    }

    too_short = len(samples) < MIN_SAMPLES
    meta = {
        "host": host,
        "user": user,
        "session_id": session.session_id,
        "started": started_wall.isoformat(timespec="seconds"),
        "duration_s": round(duration_s),
        "tick_s": args.tick,
        "warmup_s": args.warmup,
        "samples_retained": len(samples),
        "skipped_processes": session.skipped,
        "checklist": checklist,
        "method": {"gpu": method_gpu, "vram": method_vram},
        "nvml_to_engine_ratio": round(mean(ratios), 2) if ratios else None,
        "too_short": too_short,
        "gpu_available": gpu_available,
    }
    if not gpu_available:
        meta["gpu_unavailable_reason"] = gpu_reason

    doc = {"per_user": per_user, "stats": stats, "meta": meta}

    try:
        json_path, csv_path = write_outputs(out_dir, stem, samples, doc)
    except OSError as exc:
        print(f"error: cannot write to {out_dir} ({exc})", file=sys.stderr)
        return 2

    # --- Summary ---
    print("")
    print(f"{total_ticks} ticks, {len(samples)} retained after warm-up, "
          f"{session.skipped} processes skipped")
    print("")
    print(f"{'metric':<10}{'mean':>10}{'p95':>10}{'peak':>10}")
    for key in METRICS:
        row = stats[key]
        if row is None:
            print(f"{key:<10}{'--':>10}{'--':>10}{'--':>10}")
        else:
            print(f"{key:<10}{row['mean']:>10.1f}{row['p95']:>10.1f}"
                  f"{row['peak']:>10.1f}")
    print("")
    print("User Profile: " + "  ".join(
        f"{key} {'--' if per_user[key] is None else per_user[key]}"
        for key in PROFILE_KEYS))
    if too_short:
        warn(f"only {len(samples)} samples retained; fewer than {MIN_SAMPLES} "
             "is too short to be representative (meta.too_short is true)")
    if not gpu_available:
        warn("gpu and vram were not measured; the User Profile carries null "
             "for both and loadgen will fall back to its defaults for them")
    print("")
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")
    print(f"use it with: python loadgen.py --profile \"{json_path}\" --users N")
    return 0


if __name__ == "__main__":
    sys.exit(main())
