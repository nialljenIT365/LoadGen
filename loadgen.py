#!/usr/bin/env python3
"""Synthetic load generator for an Azure Virtual Desktop session host.

Holds the four Dials (GPU, VRAM, CPU, RAM) at chosen Targets so user density on
the NV12ads_A10_v5 SKU can be tested without real users.

A Target is the overall figure a Consumer (Task Manager, Perfmon, nvidia-smi)
displays -- not this script's own footprint. Each Self-check tick reads the
Actual, and the script contributes only the gap between the Baseline and the
Target. See docs/adr/0001-target-is-consumer-figure.md.

Vocabulary used throughout this file is defined in CONTEXT.md.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import gc
import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from datetime import datetime

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DIALS = ("gpu", "vram", "cpu", "ram")

TICK_SECONDS = 2.0

CPU_PERIOD = 0.10          # seconds; one busy/sleep cycle per CPU worker
GPU_PERIOD = 0.20          # seconds; one busy/sleep cycle on the GPU thread
GAIN = 0.5                 # proportional correction constant for GPU and CPU

RAM_BLOCK_BYTES = 256 * 1024 * 1024
VRAM_BLOCK_BYTES = 64 * 1024 * 1024
PAGE_BYTES = 4096

SAFE_CAP = 95.0            # ram and vram cap unless --unsafe
HARD_CAP = 100.0           # gpu and cpu cap

GPU_MATMUL_N = 2048        # square fp16 matmul edge length

DEFAULT_PER_USER = {"gpu": 4.0, "cpu": 6.0, "vram_mb": 300.0, "ram_gb": 2.5}

# Tolerance, in percentage points, before a Dial that is contributing nothing is
# reported as unable to meet its Target.
BASELINE_SLACK = 2.0


# --------------------------------------------------------------------------
# CPU worker (module level so the spawn start method can import it)
# --------------------------------------------------------------------------

def _cpu_worker(duty, stop, nice: bool) -> None:
    """Busy-loop for `duty` of every CPU_PERIOD, sleep the rest.

    `duty` is a multiprocessing.Value updated by the Self-check; the worker
    re-reads it every period so a Target change takes effect within 100 ms.
    """
    if nice:
        try:
            import psutil

            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

    clock = time.perf_counter
    try:
        while not stop.is_set():
            d = duty.value
            if d <= 0.0:
                time.sleep(CPU_PERIOD)
                continue
            busy = CPU_PERIOD * (1.0 if d > 1.0 else d)
            end = clock() + busy
            while clock() < end:
                pass
            rest = CPU_PERIOD - busy
            if rest > 0.0005:
                time.sleep(rest)
    except (KeyboardInterrupt, EOFError):
        pass


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


def clamp(value: float, cap: float) -> float:
    return 0.0 if value < 0.0 else (cap if value > cap else value)


def warn(message: str) -> None:
    print(f"warning: {message}", flush=True)


# --------------------------------------------------------------------------
# Targets file
# --------------------------------------------------------------------------

class TargetsFile:
    """The runtime control file. Written once at launch, then read every tick.

    After launch the file is the sole source of truth: editing it changes a Dial
    mid-run. Invalid JSON keeps the last good document and warns.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.doc: dict = {}
        self._bad = False

    @staticmethod
    def _tidy(value):
        """Keep the file pleasant to hand-edit: 15 rather than 15.0."""
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    def write(self, users, dials: dict, per_user: dict) -> None:
        doc = {
            "users": self._tidy(users),
            "per_user": {k: self._tidy(v) for k, v in per_user.items()},
        }
        for dial in DIALS:
            doc[dial] = self._tidy(dials[dial])
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
        self.doc = doc

    def read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
            if not isinstance(doc, dict):
                raise ValueError("top level is not an object")
        except Exception as exc:
            if not self._bad:
                warn(f"cannot read {self.path} ({exc}); keeping last good values")
                self._bad = True
            return self.doc
        if self._bad:
            print(f"{self.path} readable again", flush=True)
            self._bad = False
        self.doc = doc
        return doc


def load_profile(path: str) -> dict:
    """Read a User Profile written by profiler.py and return its per_user block.

    Only the `per_user` block is used; the profile's stats and meta are for the
    operator to read. A null value means the Profiler could not measure that
    resource -- the built-in default stands in for it.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise ValueError(f"cannot read profile {path} ({exc})") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile {path} is not valid JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise ValueError(f"profile {path}: top level is not an object")

    supplied = doc.get("per_user")
    if not isinstance(supplied, dict):
        raise ValueError(
            f"profile {path} has no per_user block; it was not written by "
            "profiler.py"
        )

    per_user = dict(DEFAULT_PER_USER)
    measured = []
    for key in DEFAULT_PER_USER:
        value = supplied.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"profile {path}: per_user.{key} is not a number")
        per_user[key] = float(value)
        measured.append(key)
    if not measured:
        raise ValueError(
            f"profile {path}: per_user has no measured values for "
            f"{', '.join(DEFAULT_PER_USER)}"
        )
    missing = [key for key in DEFAULT_PER_USER if key not in measured]
    if missing:
        warn(f"profile has no value for {', '.join(missing)}; "
             "using the built-in default for each")
    return per_user


def _number(value, field: str):
    """None passes through; anything non-numeric is rejected with a warning."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warn(f"{field} is not a number; treating as null")
        return None
    return float(value)


def resolve_targets(doc: dict, total_vram_mb, total_ram_gb: float, unsafe: bool):
    """Turn a targets document into one Target per Dial.

    A numeric Dial value is manual and wins. A null Dial is derived from
    Users x User Profile. With no Users, null reads as 0.
    """
    users = _number(doc.get("users"), "users")
    per_user = dict(DEFAULT_PER_USER)
    supplied = doc.get("per_user")
    if isinstance(supplied, dict):
        for key in DEFAULT_PER_USER:
            value = _number(supplied.get(key), f"per_user.{key}")
            if value is not None:
                per_user[key] = value
    elif supplied is not None:
        warn("per_user is not an object; using defaults")

    cap = HARD_CAP if unsafe else SAFE_CAP
    caps = {"gpu": HARD_CAP, "cpu": HARD_CAP, "vram": cap, "ram": cap}

    targets: dict[str, float] = {}
    derived: dict[str, bool] = {}
    for dial in DIALS:
        manual = _number(doc.get(dial), dial)
        if manual is not None:
            targets[dial] = clamp(manual, caps[dial])
            derived[dial] = False
            continue
        derived[dial] = users is not None
        if users is None:
            targets[dial] = 0.0
        elif dial == "gpu":
            targets[dial] = clamp(users * per_user["gpu"], caps["gpu"])
        elif dial == "cpu":
            targets[dial] = clamp(users * per_user["cpu"], caps["cpu"])
        elif dial == "ram":
            share = users * per_user["ram_gb"] / total_ram_gb * 100.0
            targets[dial] = clamp(share, caps["ram"])
        else:  # vram
            if not total_vram_mb:
                targets[dial] = 0.0
            else:
                share = users * per_user["vram_mb"] / total_vram_mb * 100.0
                targets[dial] = clamp(share, caps["vram"])
    return targets, users, derived


# --------------------------------------------------------------------------
# Consumers: reading each Actual
# --------------------------------------------------------------------------

class Nvml:
    """NVML handle for the vGPU. Provides the VRAM Actual and a display-only
    utilisation figure; see GpuEngineCounter for the gpu Actual."""

    def __init__(self) -> None:
        self.ok = False
        self.error = ""
        self.hint = ""
        self._lib = None
        self._handle = None

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
        self.ok = True
        return True

    def utilization(self):
        if not self.ok:
            return None
        try:
            return float(self._lib.nvmlDeviceGetUtilizationRates(self._handle).gpu)
        except Exception:
            return None

    def memory(self):
        """(used_bytes, total_bytes) or None."""
        if not self.ok:
            return None
        try:
            info = self._lib.nvmlDeviceGetMemoryInfo(self._handle)
            return float(info.used), float(info.total)
        except Exception:
            return None

    def used_bytes(self):
        memory = self.memory()
        return None if memory is None else memory[0]

    def shutdown(self) -> None:
        if self.ok:
            try:
                self._lib.nvmlShutdown()
            except Exception:
                pass
            self.ok = False


# --- Windows "GPU Engine" performance counter: the gpu Actual --------------

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _CounterValue)]


class GpuEngineCounter:
    """Task Manager's GPU figure, sampled on a background thread.

    This is the gpu Actual the Self-check chases. Windows reports one instance
    per process per engine; Task Manager shows the busiest engine type, so the
    instances are summed within an engine type and the largest is reported.
    Any failure yields None: the gpu Dial then holds its seeded duty and warns
    once. On the A10-8Q this counter followed a duty-cycled load to within
    about 5 points while NVML utilisation did not follow it at all.
    """

    PATH = r"\GPU Engine(*)\Utilization Percentage"

    def __init__(self) -> None:
        self.value = None
        self._stop = threading.Event()
        self._thread = None
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()

    def start(self) -> None:
        if sys.platform != "win32":
            return
        try:
            pdh = ctypes.WinDLL("pdh.dll")
            # PDH returns unsigned status codes and takes pointer-width handles;
            # both need declaring or 64-bit values are truncated.
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
                return
            if pdh.PdhAddEnglishCounterW(
                self._query, self.PATH, None, ctypes.byref(self._counter)
            ) != 0:
                return
            pdh.PdhCollectQueryData(self._query)
        except Exception:
            return
        self._pdh = pdh
        self._thread = threading.Thread(
            target=self._run, name="gpu-engine-counter", daemon=True
        )
        self._thread.start()

    def _sample(self):
        size = ctypes.c_ulong(0)
        count = ctypes.c_ulong(0)
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return None
        status = self._pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE,
            ctypes.byref(size), ctypes.byref(count), None,
        )
        if status != PDH_MORE_DATA or count.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        status = self._pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE,
            ctypes.byref(size), ctypes.byref(count), buffer,
        )
        if status != 0:
            return None
        items = ctypes.cast(
            buffer, ctypes.POINTER(_CounterItem * count.value)
        ).contents
        by_engine: dict[str, float] = {}
        for item in items:
            name = item.szName or ""
            marker = "engtype_"
            engine = name[name.rfind(marker) + len(marker):] if marker in name else "?"
            by_engine[engine] = by_engine.get(engine, 0.0) + item.FmtValue.doubleValue
        return max(by_engine.values()) if by_engine else None

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.value = self._sample()
            except Exception:
                self.value = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# --------------------------------------------------------------------------
# Load producers
# --------------------------------------------------------------------------

class CpuLoad:
    """One worker process per logical CPU, all sharing an equal duty."""

    def __init__(self, nice: bool) -> None:
        self.nice = nice
        self.workers: list = []
        self.duty = None
        self._stop = None
        self._seeded = False

    def start(self) -> None:
        ctx = mp.get_context("spawn")
        self.duty = ctx.Value("d", 0.0)
        self._stop = ctx.Event()
        for _ in range(os.cpu_count() or 1):
            proc = ctx.Process(
                target=_cpu_worker,
                args=(self.duty, self._stop, self.nice),
                daemon=True,
            )
            proc.start()
            self.workers.append(proc)
        print(
            f"cpu: {len(self.workers)} workers started"
            f"{' at below-normal priority' if self.nice else ''}",
            flush=True,
        )

    def correct(self, target: float, actual) -> None:
        """Proportional correction towards the Target."""
        if not self.workers:
            return
        if target <= 0.0:
            self.duty.value = 0.0
            self._seeded = False
            return
        if not self._seeded:
            self.duty.value = clamp(target / 100.0, 1.0)
            self._seeded = True
            return
        if actual is None:
            return
        new = self.duty.value + GAIN * (target - actual) / 100.0
        self.duty.value = clamp(new, 1.0)

    def stop(self) -> None:
        if self.duty is not None:
            self.duty.value = 0.0
        if self._stop is not None:
            self._stop.set()
        for proc in self.workers:
            proc.join(timeout=1.0)
        for proc in self.workers:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.0)
        self.workers = []


class GpuLoad:
    """fp16 matmul on a dedicated thread, duty-cycled against a 200 ms period."""

    def __init__(self) -> None:
        self.duty = 0.0
        self.ready = False
        self.error = ""
        self._stop = threading.Event()
        self._thread = None
        self._torch = None
        self._a = self._b = self._c = None
        self._seeded = False
        self._device = 0

    def start(self) -> bool:
        try:
            import torch
        except ImportError as exc:
            self.error = f"torch is not installed ({exc})"
            return False
        if not torch.cuda.is_available():
            self.error = "torch is installed but reports no CUDA device"
            return False
        self._device = torch.cuda.current_device()
        try:
            n = GPU_MATMUL_N
            self._a = torch.randn(n, n, dtype=torch.float16, device="cuda")
            self._b = torch.randn(n, n, dtype=torch.float16, device="cuda")
            self._c = torch.empty(n, n, dtype=torch.float16, device="cuda")
            torch.cuda.synchronize()
        except Exception as exc:
            self.error = f"could not allocate the GPU work tensors ({exc})"
            return False
        self._torch = torch
        self.ready = True
        self._thread = threading.Thread(target=self._run, name="gpu-load", daemon=True)
        self._thread.start()
        print("gpu: load thread started", flush=True)
        return True

    def _run(self) -> None:
        torch = self._torch
        clock = time.perf_counter
        try:  # the worker thread must own its context, not attach one lazily
            torch.cuda.set_device(self._device)
        except Exception as exc:
            warn(f"gpu work failed (cuda context: {exc}); gpu dial disabled")
            self.ready = False
            return
        while not self._stop.is_set():
            duty = self.duty
            if duty <= 0.0:
                time.sleep(GPU_PERIOD)
                continue
            busy = GPU_PERIOD * (1.0 if duty > 1.0 else duty)
            end = clock() + busy
            try:
                while clock() < end and not self._stop.is_set():
                    torch.matmul(self._a, self._b, out=self._c)
                    torch.cuda.synchronize()
            except Exception as exc:  # a dead context must not wedge the tick
                warn(f"gpu work failed ({exc}); gpu dial disabled")
                self.ready = False
                return
            rest = GPU_PERIOD - (clock() - (end - busy))
            if rest > 0.0005:
                time.sleep(rest)

    def correct(self, target: float, actual) -> None:
        if not self.ready:
            return
        if target <= 0.0:
            self.duty = 0.0
            self._seeded = False
            return
        if not self._seeded:
            self.duty = clamp(target / 100.0, 1.0)
            self._seeded = True
            return
        if actual is None:
            return
        self.duty = clamp(self.duty + GAIN * (target - actual) / 100.0, 1.0)

    def stop(self) -> None:
        self.duty = 0.0
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._a = self._b = self._c = None
        if self._torch is not None:
            try:
                self._torch.cuda.empty_cache()
            except Exception:
                pass


class VramLoad:
    """A list of 64 MB device blocks, resized directly each tick.

    On a WDDM guest cudaMalloc does not fail when the frame buffer is full: the
    driver backs further blocks with shared system memory and NVML memory.used
    stops rising. Left unchecked, the Self-check would then grow this list
    every tick into host RAM. So each new block is checked against NVML: one
    that did not raise memory.used by at least half its size landed in system
    RAM. It is freed, the Dial holds at that size, and one warning is printed.
    """

    def __init__(self) -> None:
        self.blocks: list = []
        self.ready = False
        self.error = ""
        self._torch = None
        self._ceiling = None  # block count at which the frame buffer was full

    def start(self) -> bool:
        try:
            import torch
        except ImportError as exc:
            self.error = f"torch is not installed ({exc})"
            return False
        if not torch.cuda.is_available():
            self.error = "torch is installed but reports no CUDA device"
            return False
        self._torch = torch
        self.ready = True
        return True

    @property
    def own_bytes(self) -> int:
        return len(self.blocks) * VRAM_BLOCK_BYTES

    def resize(self, wanted_bytes: int, used_bytes) -> None:
        """Grow or shrink to `wanted_bytes`.

        `used_bytes` returns NVML memory.used, or None when it cannot be read;
        without it the spill check is skipped.
        """
        torch = self._torch
        wanted_blocks = max(0, round(wanted_bytes / VRAM_BLOCK_BYTES))
        if self._ceiling is not None and wanted_blocks < self._ceiling:
            self._ceiling = None  # Target lowered; the next rise probes again
        if self._ceiling is not None:
            wanted_blocks = min(wanted_blocks, self._ceiling)
        while len(self.blocks) > wanted_blocks:
            self.blocks.pop()
        if len(self.blocks) < wanted_blocks:
            try:
                while len(self.blocks) < wanted_blocks:
                    before = used_bytes()
                    self.blocks.append(
                        torch.empty(
                            VRAM_BLOCK_BYTES, dtype=torch.uint8, device="cuda"
                        )
                    )
                    if before is not None and not self._landed(used_bytes, before):
                        self.blocks.pop()
                        torch.cuda.empty_cache()
                        self._ceiling = len(self.blocks)
                        warn(
                            f"vram allocation stopped at {self.own_bytes / 2**20:.0f} MB:"
                            " the frame buffer is full and the next block landed in"
                            " system RAM; holding here"
                        )
                        break
            except Exception as exc:
                self._ceiling = len(self.blocks)
                warn(f"vram allocation stopped at {self.own_bytes / 2**20:.0f} MB ({exc})")
        else:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    @staticmethod
    def _landed(used_bytes, before: float) -> bool:
        """True once NVML shows the block in the frame buffer; False after 1 s."""
        for _ in range(10):
            after = used_bytes()
            if after is None or after - before >= VRAM_BLOCK_BYTES // 2:
                return True
            time.sleep(0.1)
        return False

    def release(self) -> None:
        self.blocks = []
        if self._torch is not None:
            try:
                self._torch.cuda.empty_cache()
            except Exception:
                pass


class RamLoad:
    """A list of 256 MB resident blocks, filled on a background thread.

    Filling 95% of 110 GB takes roughly 20 seconds, so the work happens off the
    Self-check thread and the console keeps printing while it runs.
    """

    def __init__(self) -> None:
        self.blocks: list = []
        self._lock = threading.Lock()
        self._goal_blocks = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ram-fill", daemon=True)
        self._thread.start()

    @property
    def own_bytes(self) -> int:
        with self._lock:
            return len(self.blocks) * RAM_BLOCK_BYTES

    @property
    def progress(self):
        """(held_blocks, goal_blocks) while a resize is in flight, else None."""
        with self._lock:
            held = len(self.blocks)
        goal = self._goal_blocks
        return (held, goal) if held != goal else None

    def resize(self, wanted_bytes: int) -> None:
        self._goal_blocks = max(0, round(wanted_bytes / RAM_BLOCK_BYTES))
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    held = len(self.blocks)
                goal = self._goal_blocks
                if held == goal:
                    break
                if held > goal:
                    with self._lock:
                        self.blocks.pop()
                    if len(self.blocks) == goal:
                        gc.collect()
                    continue
                try:
                    block = bytearray(RAM_BLOCK_BYTES)
                    # Touch one byte per page so the block becomes resident and
                    # Task Manager counts it as "In use".
                    for offset in range(0, RAM_BLOCK_BYTES, PAGE_BYTES):
                        block[offset] = 1
                except MemoryError:
                    warn("ram allocation refused by the OS; holding at current size")
                    self._goal_blocks = held
                    break
                with self._lock:
                    self.blocks.append(block)

    def release(self) -> None:
        self._goal_blocks = 0
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            self.blocks = []
        gc.collect()


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def fmt_pct(value) -> str:
    return " --" if value is None else f"{value:3.0f}"


class ClampWarnings:
    """Warn once per change when a Target sits below the current Baseline."""

    def __init__(self) -> None:
        self._state: dict[str, tuple] = {}

    def check(self, dial: str, target: float, baseline) -> None:
        if baseline is None or baseline <= target + BASELINE_SLACK:
            self._state.pop(dial, None)
            return
        # Keyed on the Target alone: the Baseline drifts tick to tick and would
        # otherwise re-fire the same warning forever.
        if self._state.get(dial) == round(target, 1):
            return
        self._state[dial] = round(target, 1)
        warn(
            f"{dial} target {target:.0f} is below the current Baseline "
            f"({baseline:.0f}); contributing nothing"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="loadgen.py",
        description=(
            "Hold GPU, VRAM, CPU and RAM at chosen Targets on an AVD session "
            "host. A Target is the overall figure a Consumer displays."
        ),
    )
    for dial in DIALS:
        parser.add_argument(f"--{dial}", type=float, help=f"{dial} target, 0-100")
    parser.add_argument("--users", type=float,
                        help="simulated users; derives every dial not given explicitly")
    parser.add_argument("--duration", type=parse_duration,
                        help="run time, e.g. 90s, 30m, 2h; default is until Ctrl+C")
    parser.add_argument("--log", metavar="PATH", help="append a CSV row per tick")
    parser.add_argument("--nice", action="store_true",
                        help="run CPU workers at below-normal priority")
    parser.add_argument("--unsafe", action="store_true",
                        help="allow ram and vram targets above 95")
    parser.add_argument("--targets", metavar="PATH",
                        help="targets file path; default targets.json beside this script")
    parser.add_argument("--profile", metavar="PATH",
                        help="User Profile written by profiler.py; its per_user "
                             "block replaces the built-in defaults")
    args = parser.parse_args(argv)

    per_user = dict(DEFAULT_PER_USER)
    if args.profile:
        try:
            per_user = load_profile(args.profile)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"user profile: {args.profile}", flush=True)

    try:
        import psutil
    except ImportError:
        print("error: psutil is required. pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    cli_dials = {dial: getattr(args, dial) for dial in DIALS}
    # The GPU stack is only needed if a gpu or vram Target can be non-zero: an
    # explicit non-zero value, or a Users count that will derive one.
    wants_gpu = bool(cli_dials["gpu"]) or bool(cli_dials["vram"]) or (
        bool(args.users)
        and (cli_dials["gpu"] is None or cli_dials["vram"] is None)
    )

    # --- GPU stack, imported lazily so a CPU/RAM-only run needs no torch ---
    nvml = Nvml()
    gpu_load = GpuLoad()
    vram_load = VramLoad()
    if wants_gpu:
        if not nvml.start():
            print(f"error: a gpu or vram target was requested but {nvml.error}.",
                  file=sys.stderr)
            print(f"       {nvml.hint}, or set --gpu 0 --vram 0.", file=sys.stderr)
            return 2
        started = gpu_load.start()
        if not started:
            print(f"error: a gpu or vram target was requested but {gpu_load.error}.",
                  file=sys.stderr)
            print("       install torch from the CUDA index in the README, or set "
                  "--gpu 0 --vram 0.", file=sys.stderr)
            nvml.shutdown()
            return 2
        vram_load.start()

    total_ram_bytes = float(psutil.virtual_memory().total)
    total_ram_gb = total_ram_bytes / 2 ** 30
    memory = nvml.memory()
    total_vram_bytes = memory[1] if memory else None
    total_vram_mb = total_vram_bytes / 2 ** 20 if total_vram_bytes else None

    # --- targets.json: written from the CLI, then the sole source of truth ---
    targets_path = args.targets or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "targets.json"
    )
    targets_file = TargetsFile(targets_path)
    try:
        targets_file.write(args.users, cli_dials, per_user)
    except OSError as exc:
        print(f"error: cannot write {targets_path} ({exc})", file=sys.stderr)
        return 2
    print(f"targets file: {targets_path} (re-read every {TICK_SECONDS:.0f}s)",
          flush=True)

    engine_counter = GpuEngineCounter()
    engine_counter.start()

    cpu_load = CpuLoad(args.nice)
    cpu_load.start()
    ram_load = RamLoad()

    log_handle = None
    log_writer = None
    if args.log:
        new_file = not os.path.exists(args.log) or os.path.getsize(args.log) == 0
        log_handle = open(args.log, "a", newline="", encoding="utf-8")
        log_writer = csv.writer(log_handle)
        if new_file:
            log_writer.writerow([
                "timestamp", "users", "gpu_t", "gpu_a", "gpu_nvml",
                "vram_t", "vram_a", "cpu_t", "cpu_a", "ram_t", "ram_a",
            ])

    # Ctrl+Break should release the load as cleanly as Ctrl+C does.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(
            signal.SIGBREAK,
            lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    psutil.cpu_percent(interval=None)  # prime the delta-based reading
    warnings = ClampWarnings()
    gpu_missing_warned = False
    gpu_blind_warned = False
    deadline = time.monotonic() + args.duration if args.duration else None
    started_at = time.monotonic()

    try:
        while True:
            tick_start = time.monotonic()
            doc = targets_file.read()
            targets, users, _ = resolve_targets(
                doc, total_vram_mb, total_ram_gb, args.unsafe
            )

            # --- measure every Actual ---
            cpu_actual = psutil.cpu_percent(interval=None)
            vmem = psutil.virtual_memory()
            ram_actual = vmem.percent
            ram_in_use = float(vmem.total - vmem.available)
            # The gpu Actual is Task Manager's figure, the GPU Engine counter.
            # NVML utilisation is read for display only: on the A10-8Q it does
            # not follow this guest's work (docs/verification-findings.md).
            gpu_actual = engine_counter.value
            nvml_actual = nvml.utilization() if nvml.ok else None
            memory = nvml.memory() if nvml.ok else None
            vram_actual = memory[0] / memory[1] * 100.0 if memory else None

            # --- a dial that needs torch but has none: say so once, keep going ---
            if (targets["gpu"] or targets["vram"]) and not (
                gpu_load.ready or vram_load.ready
            ):
                if not gpu_missing_warned:
                    reason = gpu_load.error or vram_load.error or "no GPU stack"
                    warn(f"gpu/vram target set mid-run but {reason}; holding at 0")
                    gpu_missing_warned = True

            # --- no gpu Actual: the Dial runs open-loop at Target/100, say so once ---
            if targets["gpu"] > 0.0 and gpu_load.ready and gpu_actual is None:
                if not gpu_blind_warned and engine_counter.value is None:
                    warn("gpu Actual unavailable (GPU Engine counter not readable);"
                         " holding duty at Target/100 without correction")
                    gpu_blind_warned = True

            # --- correct ---
            # A Dial at 0 is left alone: no correction, no allocation and no
            # below-Baseline warning, however busy the resource already is.
            cpu_load.correct(targets["cpu"], cpu_actual)
            if targets["cpu"] > 0.0:
                warnings.check(
                    "cpu", targets["cpu"],
                    cpu_actual if cpu_load.duty.value <= 0.0 else None,
                )

            if gpu_load.ready:
                gpu_load.correct(targets["gpu"], gpu_actual)
                if targets["gpu"] > 0.0:
                    warnings.check(
                        "gpu", targets["gpu"],
                        gpu_actual if gpu_load.duty <= 0.0 else None,
                    )

            if vram_load.ready and memory and (targets["vram"] > 0.0 or vram_load.blocks):
                baseline = memory[0] - vram_load.own_bytes
                wanted = targets["vram"] / 100.0 * memory[1] - baseline
                if targets["vram"] > 0.0:
                    warnings.check(
                        "vram", targets["vram"], baseline / memory[1] * 100.0
                    )
                vram_load.resize(int(wanted), nvml.used_bytes)

            if targets["ram"] > 0.0 or ram_load.own_bytes:
                ram_baseline = ram_in_use - ram_load.own_bytes
                if targets["ram"] > 0.0:
                    warnings.check(
                        "ram", targets["ram"],
                        ram_baseline / total_ram_bytes * 100.0,
                    )
                ram_load.resize(
                    int(targets["ram"] / 100.0 * total_ram_bytes - ram_baseline)
                )

            # --- report ---
            stamp = datetime.now().strftime("%H:%M:%S")
            parts = [f"{stamp} "]
            if users is not None:
                parts.append(f" users {users:3.0f} |")
            parts.append(
                f" gpu {fmt_pct(targets['gpu'])}/{fmt_pct(gpu_actual)}"
                f" (nvml {fmt_pct(nvml_actual)}) |"
                f" vram {fmt_pct(targets['vram'])}/{fmt_pct(vram_actual)} |"
                f" cpu {fmt_pct(targets['cpu'])}/{fmt_pct(cpu_actual)} |"
                f" ram {fmt_pct(targets['ram'])}/{fmt_pct(ram_actual)}"
            )
            filling = ram_load.progress
            if filling:
                parts.append(f"  ram fill {filling[0]}/{filling[1]} blocks")
            print("".join(parts), flush=True)

            if log_writer is not None:
                log_writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    "" if users is None else f"{users:g}",
                    f"{targets['gpu']:.1f}",
                    "" if gpu_actual is None else f"{gpu_actual:.1f}",
                    "" if nvml_actual is None else f"{nvml_actual:.1f}",
                    f"{targets['vram']:.1f}",
                    "" if vram_actual is None else f"{vram_actual:.1f}",
                    f"{targets['cpu']:.1f}",
                    "" if cpu_actual is None else f"{cpu_actual:.1f}",
                    f"{targets['ram']:.1f}",
                    f"{ram_actual:.1f}",
                ])
                log_handle.flush()

            if deadline is not None and time.monotonic() >= deadline:
                print(f"duration reached after {time.monotonic() - started_at:.0f}s",
                      flush=True)
                break

            rest = TICK_SECONDS - (time.monotonic() - tick_start)
            if deadline is not None:
                rest = min(rest, max(0.0, deadline - time.monotonic()))
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        gpu_load.stop()
        cpu_load.stop()
        vram_load.release()
        ram_load.release()
        engine_counter.stop()
        nvml.shutdown()
        if log_handle is not None:
            log_handle.close()
        print("released: cpu workers stopped, ram and vram blocks dropped", flush=True)
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
