# loadgen build brief

Build brief for the synthetic load generator. Read `CONTEXT.md` for vocabulary and `docs/adr/0001-target-is-consumer-figure.md` for the one non-obvious decision. Every design choice below was settled in a grilling session on 2026-09-09 and is not open for re-litigation; open items are listed at the end.

## Purpose

Hold GPU, VRAM, CPU and RAM on an Azure Virtual Desktop session host at chosen levels so user density on the NV12ads_A10_v5 SKU can be tested without real users. One real human session may sit alongside the synthetic load to judge responsiveness.

## Host

- NV12ads_A10_v5: Windows 10/11 multi-session, 12 vCPU, 110 GB RAM, one third of an NVIDIA A10 as an A10-8Q vGPU (8 GB frame buffer).
- GRID guest driver installed, `nvidia-smi` on PATH. No host-level power or clock control.
- Dedicated test host, no users on it. Script runs interactively in one logged-in session.
- Python 3.12 to be installed via winget (`winget install Python.Python.3.12`). Not present today.

## Consumers (what must show the right number)

Task Manager, Windows performance counters (Perfmon, Resource Monitor) and `nvidia-smi`. Nerdio and Azure Monitor parity is not required.

## Deliverables

All at the repo root unless stated.

1. `loadgen.py` — single file, Python 3.12, standard library plus `torch`, `psutil`, `nvidia-ml-py` (NVML). No compiled binaries. Must run without the GPU packages present if GPU and VRAM dials are both 0 (import lazily, fail with a clear message only when a GPU dial is non-zero).
2. `targets.json` — runtime control file, written by the script at launch from CLI arguments (see Control).
3. `README.md` — install steps for the Windows host, one-line launch example, how to change each dial mid-run, how `--users` works, and a short note on what GPU utilization does and doesn't represent on a time-sliced vGPU slice (see vGPU note).
4. `requirements.txt` — pinned as far as practical; torch index URL documented in README not in this file.

## Dials

Four dials, each a percentage 0 to 100. A dial at 0 means leave that resource alone: no workers, no allocation, no measurement-driven correction.

| Dial | What it holds | Actual read from |
|---|---|---|
| gpu | vGPU compute utilization (kernel busy time) | NVML `nvmlDeviceGetUtilizationRates().gpu` |
| vram | share of vGPU dedicated memory allocated | NVML `nvmlDeviceGetMemoryInfo()` used / total |
| cpu | total CPU across all logical processors | `psutil.cpu_percent(interval=None)` sampled each tick |
| ram | physical memory in use | `psutil.virtual_memory()` percent (matches Task Manager "In use") |

Target means the overall Consumer figure, not the script's own footprint (ADR-0001). Each tick the script measures Actual, computes gap = target minus actual, and adjusts its own contribution. Target below current Baseline: contribute nothing, print a warning once per change, keep running.

Caps: ram and vram clamp to 95 unless `--unsafe` is passed. gpu and cpu may reach 100.

## Load mechanics

### CPU

- One worker process per logical CPU (`os.cpu_count()`), spawned with `multiprocessing` (spawn context, `daemon=True` so they die with the parent).
- Each worker runs a fixed period of 100 ms: busy-loop for `duty` fraction, `time.sleep` for the rest. Duty is shared via `multiprocessing.Value` and updated by the self-check.
- Equal duty on every worker so per-core graphs look uniform.
- Priority: normal by default. `--nice` sets workers to below-normal (`psutil.Process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)`).
- Correction: proportional. `duty += k * (target - actual) / 100`, clamp 0 to 1, k about 0.5. Seed duty at `target / 100` on first tick so it starts close.

### GPU

- Runs in the main process on a dedicated thread. Period 200 ms. Work unit: `torch.matmul` on two 2048×2048 fp16 tensors held on the device, followed by `torch.cuda.synchronize()`. Repeat until `duty * period` elapsed, then sleep the remainder.
- Same proportional correction as CPU, chasing NVML utilization.
- Also read the Windows `GPU Engine` performance counter total (via `psutil` is not enough; use `ctypes` PDH or `wmi`-free `subprocess` to `typeperf` once per tick, tolerate failure) and print it beside the NVML figure so divergence between nvidia-smi and Task Manager is visible. This figure is display-only, never chased.

### VRAM

- Hold a list of `torch.empty(64 MB, dtype=uint8, device='cuda')` blocks. Each tick compute needed bytes = (target/100 × total) − (used − own), then append or pop blocks. Call `torch.cuda.empty_cache()` after popping so the driver actually sees the release.
- No duty cycle. Direct computation each tick.

### RAM

- Hold a list of 256 MB `bytearray` blocks in the main process. Touch every 4 KB page on allocation (write one byte per page) so it becomes resident and counts as "In use".
- Each tick compute needed bytes the same way as VRAM: (target/100 × total physical) − (used − own). Append or pop blocks, `gc.collect()` after popping.
- Large fills are slow (95% of 110 GB takes roughly 20 s). Allocate in a background thread so the self-check keeps printing; show progress.

## Control

- CLI: `--gpu N --vram N --cpu N --ram N --users N --duration 90s|30m|2h --log path.csv --nice --unsafe --targets path`. Dials default to 0. `--users` defaults to none.
- At launch the script writes `targets.json` next to itself (or at `--targets`), overwriting any existing file, from the CLI values. After that the file is the sole source of truth. It is re-read every 2 s. Invalid JSON: keep the last good values and print a warning.
- File shape:

```json
{
  "users": 15,
  "per_user": { "gpu": 4, "cpu": 6, "vram_mb": 300, "ram_gb": 2.5 },
  "gpu": null,
  "vram": null,
  "cpu": null,
  "ram": null
}
```

- `users` × `per_user` derives every dial whose value is `null`. A numeric dial value is manual and overrides the derived one. `users` absent or null means all dials are manual and `null` reads as 0.
- Derivation: gpu = users × per_user.gpu; cpu = users × per_user.cpu; vram = users × per_user.vram_mb / total_vram_mb × 100; ram = users × per_user.ram_gb / total_ram_gb × 100. Clamp results the same way as manual values.
- Default `per_user` when not supplied: gpu 4, cpu 6, vram_mb 300, ram_gb 2.5. These are guesses; the README tells the operator to measure one real user and replace them.

## Self-check and output

- Tick every 2 s. Each tick: re-read targets, measure all four Actuals, correct GPU and CPU duty, resize VRAM and RAM, print one console line.
- Console line format, fixed width, one per tick:

```
12:34:56  users 15 | gpu 60/58 (tm 55) | vram 52/51 | cpu 90/88 | ram 35/35
```

  target/actual per dial, `tm` = GPU Engine counter figure, `users` shown only when set.
- `--log path.csv` appends `timestamp, users, gpu_t, gpu_a, gpu_tm, vram_t, vram_a, cpu_t, cpu_a, ram_t, ram_a` per tick. Off by default.

## Lifecycle

- `--duration` optional, suffix s/m/h. Without it, run until Ctrl+C.
- On exit (duration reached, Ctrl+C, or unhandled error): set all duties to 0, terminate and join CPU workers, drop RAM blocks, drop VRAM blocks and `empty_cache`, `nvmlShutdown`. Exit within 5 s. Print a final line confirming release.

## Install steps (README content)

1. `winget install Python.Python.3.12` then open a new shell.
2. `nvidia-smi` and read the "CUDA Version" in the header. If 12.6 or newer, torch index `https://download.pytorch.org/whl/cu126`; otherwise `cu124`. Both run on GRID R535 and newer via CUDA minor-version compatibility. A10 is sm_86 and torch ships native kernels for it.
3. `py -3.12 -m venv .venv` and activate.
4. `pip install torch --index-url <chosen index>` then `pip install -r requirements.txt` for psutil and nvidia-ml-py.
5. Launch example: `python loadgen.py --gpu 40 --vram 25 --cpu 60 --ram 70 --duration 30m --log run1.csv`
6. Users example: `python loadgen.py --users 15`

## vGPU note (README content)

On an A10-8Q slice the physical GPU is time-sliced between three VMs. NVML utilization inside the guest reports the share of scheduling time this vGPU was running a kernel, not the share of the physical A10. When neighbours are busy your kernels take longer wall-clock time but the percentage still reads against your own time slices, so the figure is about your slice's saturation, not the card's. Task Manager's GPU Engine percentage comes from the Windows display driver scheduler and can differ from NVML by 10 to 20 points; the script chases NVML and prints both. Neither figure is a per-user measure; that is what the User Profile is for.

## Verification

Development machine has no NVIDIA GPU. Verify here:

- CPU dial at 30, 60, 90: `psutil.cpu_percent` settles within ±5 of target inside 10 s.
- RAM dial at 40: `virtual_memory().percent` settles within ±2. Then edit file to 20: blocks released, figure drops.
- Bad JSON in the file: warning printed, last values kept.
- `--duration 10s`: exits cleanly, workers gone (`tasklist` shows no orphan python), RAM released.
- Ctrl+C mid-fill: same clean exit.
- GPU dials at 0 with torch absent: script runs.
- GPU dial non-zero with torch absent: clear error, no traceback wall.

Verify on the AVD host only (operator does this):

- GPU dial at 50: nvidia-smi and Task Manager both move; console shows target/actual converge.
- VRAM dial at 50: nvidia-smi `memory.used` about 4 GB.
- `--users 15` with default profile: all four dials populated and held.

## Out of scope

- Multiple fake user sessions, test accounts, automated logons.
- Nerdio or Azure Monitor counter parity.
- Disk or network load.
- Any host-level GPU control.

## Open items for the operator, not the builder

- Measure a real single user and replace the default `per_user` values.
- Confirm GRID driver CUDA level on the host before choosing the torch index.
