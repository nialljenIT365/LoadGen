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
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time

N = 2048  # must match GPU_MATMUL_N in loadgen.py
FLOP_PER_MATMUL = 2.0 * N * N * N


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
    if args.vram_fill > 0.0 and handle is not None:
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        want = info.total * args.vram_fill / 100.0
        block = 64 * 1024 * 1024
        while pynvml.nvmlDeviceGetMemoryInfo(handle).used < want:
            try:
                blocks.append(torch.empty(block, dtype=torch.uint8, device="cuda"))
            except RuntimeError as exc:
                print(f"vram fill stopped early ({exc})")
                break
        used = pynvml.nvmlDeviceGetMemoryInfo(handle).used / 1048576.0
        print(f"vram filled to {used:.0f} MiB before the probe started")

    a = torch.randn(N, N, dtype=torch.float16, device="cuda")
    b = torch.randn(N, N, dtype=torch.float16, device="cuda")
    c = torch.empty(N, N, dtype=torch.float16, device="cuda")
    torch.cuda.synchronize()

    samples = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            if handle is not None:
                samples.append(sample_nvml(handle, pynvml))
            stop.wait(0.5)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    # warm up: the first matmul pays cuBLAS handle and workspace setup
    for _ in range(5):
        torch.matmul(a, b, out=c)
    torch.cuda.synchronize()
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
    if samples:
        print(f"nvml util.gpu   {summarise(samples, 'util_gpu')}")
        print(f"nvml util.mem   {summarise(samples, 'util_mem')}")
        print(f"nvml clocks.sm  {summarise(samples, 'clock_sm')} MHz")
        print(f"nvml power      {summarise(samples, 'power_w')} W")
        print(f"nvml temp       {summarise(samples, 'temp_c')} C")
        print(f"nvml mem.used   {summarise(samples, 'mem_used_mb')} MiB")

    print()
    utils = [s["util_gpu"] for s in samples if s["util_gpu"] is not None]
    busy = tflops > 1.0
    if not utils:
        print("VERDICT: NVML reports no utilisation figure on this host at all.")
        print("         The gpu Dial cannot close its loop here. Drive it open-loop")
        print("         and say so in README.md and GUIDE.md.")
    elif busy and statistics.mean(utils) < 10.0:
        print("VERDICT: the GPU is doing real work and NVML utilisation is wrong.")
        print("         The defect is in the instrument, not the load thread.")
        print("         loadgen.py must stop closing its loop on this reading.")
    elif busy:
        print("VERDICT: throughput and NVML agree. The instrument is sound here;")
        print("         reproduce the collapse with --vram-fill 95 before concluding.")
    else:
        print("VERDICT: throughput is near zero. The load itself is failing, not the")
        print("         measurement. Look at GpuLoad._run, not at NVML.")

    del blocks
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
