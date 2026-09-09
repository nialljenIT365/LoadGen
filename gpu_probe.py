"""Decide whether the GPU is idle or whether NVML utilisation is lying.

Runs the same 2048 fp16 matmul that loadgen.py uses, flat out, on a dedicated
thread with the CUDA device set explicitly. While it runs, it samples NVML
utilisation, SM clock, power draw and temperature.

If achieved throughput is a plausible fraction of the card's peak while NVML
reports 0% utilisation, the instrument is wrong and the load is fine. If
throughput collapses and the SM clock sits at idle, the load is wrong.

    python gpu_probe.py            # 15 s
    python gpu_probe.py --seconds 60 --vram-fill 95

--vram-fill reproduces the condition of the 14:47 capture, where the vram Dial
was holding 95% of an 8 GB frame buffer while the gpu Dial fell to 0.

The fill stops early if a block lands in shared system memory instead of the
frame buffer. On a WDDM guest cudaMalloc does not fail when the frame buffer
is full; the driver spills into host RAM and NVML memory.used stops rising.
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time

N = 2048  # must match GPU_MATMUL_N in loadgen.py
FLOP_PER_MATMUL = 2.0 * N * N * N
BLOCK = 64 * 1024 * 1024
HEADROOM = 256 * 1024 * 1024  # kept free for the work tensors and cuBLAS workspace
SAMPLE_SECONDS = 0.2
MIB = 1048576.0


def sample_nvml(handle, pynvml):
    """One reading of every NVML field worth having. Missing fields are None."""
    out = {}
    for key, fn in (
        ("util_gpu", lambda: pynvml.nvmlDeviceGetUtilizationRates(handle).gpu),
        ("util_mem", lambda: pynvml.nvmlDeviceGetUtilizationRates(handle).memory),
        ("clock_sm", lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)),
        ("power_w", lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0),
        ("temp_c", lambda: pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)),
        ("mem_used_mb", lambda: pynvml.nvmlDeviceGetMemoryInfo(handle).used / 1048576.0),
    ):
        try:
            out[key] = float(fn())
        except Exception:
            out[key] = None
    return out


def summarise(samples, key):
    values = [s[key] for s in samples if s[key] is not None]
    if not values:
        return "unavailable"
    if len(values) == 1:
        return f"{values[0]:.1f}"
    return (
        f"min {min(values):.1f}  mean {statistics.mean(values):.1f}  "
        f"max {max(values):.1f}"
    )


def histogram(values):
    """Bucket counts, so a bimodal 0/100 counter is told apart from a noisy one."""
    buckets = ((0, 0, "0"), (1, 24, "1-24"), (25, 49, "25-49"),
               (50, 74, "50-74"), (75, 99, "75-99"), (100, 100, "100"))
    return "  ".join(
        f"{label}:{sum(1 for v in values if lo <= v <= hi)}"
        for lo, hi, label in buckets
    )


def fill_vram(torch, pynvml, handle, percent):
    """Hold 64 MiB blocks until NVML memory.used reaches `percent` of total.

    Each block is checked: if memory.used did not rise by at least half a block
    within a second, the block went to shared system memory. It is freed and
    the fill stops there. Returns (blocks, reason).
    """
    total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
    want = total * percent / 100.0
    blocks = []
    while True:
        used = pynvml.nvmlDeviceGetMemoryInfo(handle).used
        if used >= want:
            return blocks, "reached the target"
        if total - used < BLOCK + HEADROOM:
            return blocks, f"kept {HEADROOM / MIB:.0f} MiB free for the work tensors"
        try:
            blocks.append(torch.empty(BLOCK, dtype=torch.uint8, device="cuda"))
        except RuntimeError as exc:
            return blocks, f"allocation failed ({str(exc).splitlines()[0][:90]})"
        for _ in range(10):
            after = pynvml.nvmlDeviceGetMemoryInfo(handle).used
            if after - used >= BLOCK // 2:
                break
            time.sleep(0.1)
        else:
            blocks.pop()
            torch.cuda.empty_cache()
            return blocks, ("the next block landed in shared system memory, not the"
                            " frame buffer; cudaMalloc oversubscribes on this host")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--vram-fill", type=float, default=0.0,
                    help="fill the frame buffer to this percent before starting")
    args = ap.parse_args()

    import torch

    if not torch.cuda.is_available():
        print("torch reports no CUDA device")
        return 2

    device = torch.cuda.current_device()
    torch.cuda.set_device(device)
    print(f"device: {torch.cuda.get_device_name(device)}")

    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception as exc:
        print(f"NVML unavailable ({exc}); throughput will still be measured")
        pynvml = handle = None

    blocks = []
    filled_mib = None
    if args.vram_fill > 0.0 and handle is not None:
        blocks, reason = fill_vram(torch, pynvml, handle, args.vram_fill)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        filled_mib = info.used / MIB
        print(f"vram fill stopped at {filled_mib:.0f} MiB of {info.total / MIB:.0f}"
              f" ({filled_mib / (info.total / MIB) * 100:.0f}%), holding"
              f" {len(blocks) * BLOCK / MIB:.0f} MiB in {len(blocks)} blocks: {reason}")

    try:
        a = torch.randn(N, N, dtype=torch.float16, device="cuda")
        b = torch.randn(N, N, dtype=torch.float16, device="cuda")
        c = torch.empty(N, N, dtype=torch.float16, device="cuda")
        torch.cuda.synchronize()
    except Exception as exc:
        print(f"could not allocate the work tensors: {str(exc).splitlines()[0][:120]}")
        return 3

    samples = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            if handle is not None:
                samples.append(sample_nvml(handle, pynvml))
            stop.wait(SAMPLE_SECONDS)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    # warm up: the first matmul pays cuBLAS handle and workspace setup
    try:
        for _ in range(5):
            torch.matmul(a, b, out=c)
        torch.cuda.synchronize()
    except Exception as exc:
        stop.set()
        print(f"warm-up matmul failed: {str(exc).splitlines()[0][:120]}")
        return 3
    samples.clear()

    print(f"running {N}x{N} fp16 matmul flat out for {args.seconds:.0f}s ...")
    iters = 0
    started = time.perf_counter()
    deadline = started + args.seconds
    per_second = []
    mark, mark_iters = started, 0
    try:
        while time.perf_counter() < deadline:
            torch.matmul(a, b, out=c)
            torch.cuda.synchronize()
            iters += 1
            now = time.perf_counter()
            if now - mark >= 1.0:
                per_second.append((iters - mark_iters) / (now - mark))
                mark, mark_iters = now, iters
    except Exception as exc:
        print(f"matmul failed after {iters} iterations: {exc}")
        stop.set()
        return 3
    elapsed = time.perf_counter() - started
    stop.set()
    poller.join(timeout=2.0)

    tflops = iters * FLOP_PER_MATMUL / elapsed / 1e12
    print()
    print(f"iterations      {iters} in {elapsed:.1f}s "
          f"({iters / elapsed:.1f}/s, {elapsed / iters * 1000:.2f} ms each)")
    print(f"throughput      {tflops:.1f} TFLOP/s fp16")
    if per_second:
        first, last = per_second[0], per_second[-1]
        print(f"rate first/last {first:.1f}/s -> {last:.1f}/s "
              f"({'steady' if last > first * 0.7 else 'DECAYING'})")
    print()
    utils = [s["util_gpu"] for s in samples if s["util_gpu"] is not None]
    if samples:
        print(f"nvml util.gpu   {summarise(samples, 'util_gpu')}"
              f"  ({len(utils)} samples every {SAMPLE_SECONDS:.1f}s)")
        if len(utils) > 1:
            print(f"     sd {statistics.pstdev(utils):.1f}   buckets {histogram(utils)}")
        print(f"nvml util.mem   {summarise(samples, 'util_mem')}")
        print(f"nvml clocks.sm  {summarise(samples, 'clock_sm')} MHz")
        print(f"nvml power      {summarise(samples, 'power_w')} W")
        print(f"nvml temp       {summarise(samples, 'temp_c')} C")
        print(f"nvml mem.used   {summarise(samples, 'mem_used_mb')} MiB")

    print()
    busy = tflops > 1.0
    where = f" with the frame buffer at {filled_mib:.0f} MiB" if filled_mib else ""
    if not utils:
        print("VERDICT: NVML reports no utilisation figure on this host at all.")
        print("         The gpu Dial cannot close its loop here. Drive it open-loop")
        print("         and say so in README.md and GUIDE.md.")
    elif not busy:
        print(f"VERDICT: throughput is near zero{where}. The load itself is failing,")
        print("         not the measurement. Look at GpuLoad._run, not at NVML.")
    else:
        mean = statistics.mean(utils)
        sd = statistics.pstdev(utils) if len(utils) > 1 else 0.0
        if mean < 10.0:
            print(f"VERDICT: the GPU is doing real work{where} and NVML reads idle.")
            print("         The defect is in the instrument, not the load thread.")
            print("         loadgen.py must stop closing its loop on this reading.")
        elif mean < 80.0 or sd > 20.0:
            print(f"VERDICT: the GPU was busy for the whole run{where}, yet NVML")
            print(f"         utilisation read {mean:.0f} +/- {sd:.0f}. A counter that")
            print("         tracked this guest's work would sit near 100 with little")
            print("         spread. This one does not follow the load; the gpu Dial")
            print("         cannot close its loop on it.")
        else:
            print(f"VERDICT: throughput and NVML agree{where}. The instrument is")
            print("         sound here; reproduce the collapse with --vram-fill 95")
            print("         before concluding.")

    del blocks
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
