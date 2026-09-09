"""Decide whether the GPU is idle or whether NVML utilisation is lying.

Runs the same 2048 fp16 matmul that loadgen.py uses, flat out, on a dedicated
thread with the CUDA device set explicitly. While it runs, it samples NVML
utilisation, SM clock, power draw and temperature.

If achieved throughput is a plausible fraction of the card's peak while NVML
reports 0% utilisation, the instrument is wrong and the load is fine. If
throughput collapses and the SM clock sits at idle, the load is wrong.

    python gpu_probe.py            # 15 s, flat out
    python gpu_probe.py --seconds 60 --vram-fill 95
    python gpu_probe.py --seconds 30 --duty 0.7   # the busy/sleep cycle GpuLoad uses

While it runs it also reads Task Manager's GPU Engine counter, the `tm`
column in loadgen.py, so the two candidate feedback signals are judged
against the same load. --duty is for that comparison: a signal that can
carry the gpu Dial reads near duty x 100 with little spread.

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
    ap.add_argument("--duty", type=float, default=1.0,
                    help="fraction of each 200 ms period to run, as GpuLoad does;"
                         " 1.0 is flat out")
    args = ap.parse_args()
    duty = min(max(args.duty, 0.05), 1.0)

    import torch
    from loadgen import GPU_PERIOD, GpuEngineCounter

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
    tm_samples = []  # Task Manager's GPU Engine counter, read once a second
    stop = threading.Event()
    engine = GpuEngineCounter()
    engine.start()

    def poll():
        n = 0
        while not stop.is_set():
            if handle is not None:
                samples.append(sample_nvml(handle, pynvml))
            if n % 5 == 0 and engine.value is not None:
                tm_samples.append(float(engine.value))
            n += 1
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
    tm_samples.clear()

    how = "flat out" if duty >= 1.0 else f"at duty {duty:.2f} of each {GPU_PERIOD * 1000:.0f} ms"
    print(f"running {N}x{N} fp16 matmul {how} for {args.seconds:.0f}s ...")
    iters = 0
    clock = time.perf_counter
    started = clock()
    deadline = started + args.seconds
    per_second = []
    mark, mark_iters = started, 0
    try:
        while clock() < deadline:
            # the same busy/sleep cycle as GpuLoad._run
            busy = GPU_PERIOD * duty
            cycle_start = clock()
            end = cycle_start + busy
            while clock() < end:
                torch.matmul(a, b, out=c)
                torch.cuda.synchronize()
                iters += 1
            now = clock()
            if now - mark >= 1.0:
                per_second.append((iters - mark_iters) / (now - mark))
                mark, mark_iters = now, iters
            rest = GPU_PERIOD - (now - cycle_start)
            if duty < 1.0 and rest > 0.0005:
                time.sleep(rest)
    except Exception as exc:
        print(f"matmul failed after {iters} iterations: {exc}")
        stop.set()
        engine.stop()
        return 3
    elapsed = clock() - started
    stop.set()
    poller.join(timeout=2.0)
    engine.stop()

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

    if tm_samples:
        print(f"task manager    min {min(tm_samples):.1f}  mean {statistics.mean(tm_samples):.1f}"
              f"  max {max(tm_samples):.1f}  sd {statistics.pstdev(tm_samples):.1f}"
              f"  ({len(tm_samples)} samples every 1s; the tm column in loadgen.py)")
    else:
        print("task manager    unavailable (GPU Engine counter not readable here)")

    print()
    busy = tflops > 1.0 * duty
    expected = duty * 100.0
    where = f" with the frame buffer at {filled_mib:.0f} MiB" if filled_mib else ""

    def follows(values):
        """A signal is usable as feedback if it sits near the duty with little spread."""
        mean = statistics.mean(values)
        sd = statistics.pstdev(values) if len(values) > 1 else 0.0
        return abs(mean - expected) <= 10.0 and sd <= 15.0, mean, sd

    if not utils:
        print("VERDICT: NVML reports no utilisation figure on this host at all.")
    elif not busy:
        print(f"VERDICT: throughput is near zero{where}. The load itself is failing,")
        print("         not the measurement. Look at GpuLoad._run, not at NVML.")
    else:
        ok, mean, sd = follows(utils)
        if ok:
            print(f"VERDICT: NVML utilisation follows the load{where}: read {mean:.0f}"
                  f" +/- {sd:.0f}")
            print(f"         against a duty of {expected:.0f}. The instrument is sound here.")
        elif expected >= 50.0 and mean < 10.0:
            print(f"VERDICT: the GPU is doing real work{where} and NVML reads idle.")
            print("         The defect is in the instrument, not the load thread.")
        else:
            print(f"VERDICT: the GPU ran at a duty of {expected:.0f} for the whole run{where},")
            print(f"         yet NVML utilisation read {mean:.0f} +/- {sd:.0f}. A counter that")
            print(f"         tracked this guest's work would sit near {expected:.0f} with little")
            print("         spread. This one does not follow the load; the gpu Dial")
            print("         cannot close its loop on it.")
    if busy and tm_samples:
        ok, mean, sd = follows(tm_samples)
        if ok:
            print(f"         Task Manager's GPU Engine counter (tm) read {mean:.0f} +/- {sd:.0f}")
            print(f"         against a duty of {expected:.0f}: it follows the load and")
            print("         can carry the gpu Dial's feedback instead.")
        else:
            print(f"         Task Manager's GPU Engine counter (tm) read {mean:.0f} +/- {sd:.0f}")
            print(f"         against a duty of {expected:.0f}: it does not follow the load")
            print("         either.")

    del blocks
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
