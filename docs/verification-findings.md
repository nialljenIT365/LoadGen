# LoadGen verification findings — NV12ads_A10_v5

Host: an NV12ads_A10_v5 session host, NVIDIA A10-8Q, 8192 MiB frame buffer,
12 logical CPUs, 110 GB RAM. Measured on 2026-09-09 by relay: commands run on
the host over RDP, output read on a separate machine. Code under test starts at
`56076f1` and ends at `e3b93c2`; the fixes below landed during the session.

Vocabulary follows `CONTEXT.md`. Method follows `docs/verification-runbook.md`.

## Step 0 — is the gpu Actual trustworthy on this host

**As shipped at `56076f1`, no.** The gpu Actual was NVML utilisation, and on
this host NVML utilisation does not follow the guest's own work. `gpu_probe.py`
ran the same 2048 fp16 matmul `loadgen.py` uses, at a fixed duty, and read NVML
and Task Manager's GPU Engine counter side by side:

| Duty | Throughput | NVML util.gpu | GPU Engine counter (`tm`) |
|---|---|---|---|
| 0.3 | 5.5 TFLOP/s, steady | 1 ± 3, 100 of 149 samples at 0 | 28 ± 1 |
| 0.7 | 12.8 TFLOP/s, steady | 33 ± 41 | 65 ± 1 |
| 1.0 | 18.0 TFLOP/s, steady | 46 ± 43 | 91 ± 2 |
| 1.0, frame buffer at 96% | 17.7 TFLOP/s, steady for 60 s | 39 ± 44 | not sampled |

Three flat-out runs read NVML at 52, 39 and 46 with a spread of 43 to 45 each
time, swinging between 0 and 100 within seconds. At a duty of 0.3 it read near
zero for two thirds of the samples. The mean moved between identical runs, so
it is driven by something outside the guest. It is not this guest's kernel-busy
fraction and cannot carry a feedback loop at 2 s ticks.

The load itself is sound. Throughput was the same with the frame buffer at 96%
as with it empty, without decay over 60 s. The 14:47 collapse was the counter,
not the load thread, and `81178ca` (CUDA context bound on the load thread) ran
without a warning throughout.

`clocks.sm` and `temp` return 0 on this profile; `power` is unavailable. The
driver returns 0 rather than an error for what it withholds, which is the same
habit that made a utilisation 0 ambiguous.

**What the gpu Dial honestly claims, from `1ce4f25`:** it holds Task Manager's
GPU figure, the Windows `\GPU Engine(*)\Utilization Percentage` counter summed
per engine type with the busiest reported. That counter followed duty linearly
at 28 / 65 / 91 against 30 / 70 / 100, and it is the figure density testers
read. NVML utilisation is kept as a display-only column (`gpu_nvml` in the CSV,
`(nvml n)` on the console). `nvidia-smi` reads the same NVML counter, so it is
not a cross-check for the gpu Dial on this host.

Limit: the engine counter reads about 0.9 × duty, so with duty pinned at 1.0 a
gpu Target above about 90 reads 91. Inside the ±10 tolerance, but a Target of
100 is not reachable as a displayed figure.

### Why it was a correctness defect

`GpuLoad.correct()` treated a hard 0 from NVML as an idle GPU and raised duty
by GAIN × (Target − 0) every tick. At any Target below 100 it drove duty to 1.0
while printing a low Actual: at a Target of 40 it burned the whole vGPU and
believed it was undershooting. Fixed by sourcing the Actual from the engine
counter. When that counter cannot be read the Dial now holds its seeded duty
and prints one warning rather than correcting on nothing.

## Defects found and fixed

| Commit | Defect | Evidence |
|---|---|---|
| `0395bf2` | The vram Dial spilled into system RAM once the frame buffer was full. `cudaMalloc` on this WDDM guest does not fail at the frame buffer limit; the driver backs further blocks with shared system memory and NVML `memory.used` stops rising, so `VramLoad.resize` computed an ever more negative Baseline and grew every tick. | `gpu_probe.py --vram-fill 95` at `56076f1`: 93.75 GiB allocated by PyTorch, Task Manager Shared GPU memory 88 GB, system RAM 99 of 110 GB. |
| `0395bf2` | `gpu_probe.py` called NVML "sound" on mean 29.6 with min 0 and max 100 under a flat-out load. | First step 0 run. |
| `1ce4f25` | gpu Actual sourced from NVML utilisation; see step 0. | Table above. |

The spill guard checks each new 64 MiB block against NVML `memory.used`; a
block that does not raise it by half its size within a second is freed and the
Dial holds there with one warning. A 95% fill on the patched probe reached
7832 MiB in 96 blocks without triggering it, so at the `SAFE_CAP` of 95 the
guard is a safety net rather than the normal path.

## NVML to Task Manager divergence

Not the 10 to 20 point offset the runbook anticipated. On this profile the two
measure different things: the engine counter is the guest-side busy fraction
and tracks duty; NVML utilisation is a driver-side figure that swings 0 to 100
under constant load and moves between identical runs. The runbook's "what not
to conclude" note, that NVML reports this vGPU's share of its own scheduling
time, is not supported by the data either; whatever it reports, it is not a
stable function of the guest's load.

## Per Dial

### gpu

- Agrees with an independent Consumer: yes, from `1ce4f25`. The Actual is
  Task Manager's own counter, read through PDH.
- Converges, holds, releases: pending the closed-loop run below.

### vram

- Fill to 95% reached cleanly: 7832 MiB of 8192 in 96 blocks.
- Agreement, convergence, hold, release: pending step 3.

### cpu, ram

Pending steps 3 to 6.

## Pending

Everything below is still to be executed on the host, following the runbook.
The code is at `e3b93c2` or later; `git pull` on the host before each run.

1. **Closed-loop gpu run.** `python loadgen.py --gpu 40 --duration 3m --log analysis\run-<date>-<time>-gpu40.csv`, then `analyse_csv.py` on it. Pass is convergence within 20 s and ±10 steady state. First execution of `1ce4f25` on real hardware.
2. **Step 1 Baseline.** Two minutes idle: NVML util and memory.used range, Task Manager CPU, Memory, Dedicated and Shared GPU memory. Also confirms the spilled RAM from the first `--vram-fill 95` run was released after the process was killed.
3. **Step 2.** Cross-check each Dial against its Consumer per the runbook table. For gpu that is now Task Manager and Perfmon, not `nvidia-smi`.
4. **Steps 3 to 7.** Single-Dial runs, the 30-minute all-Dials run, Baseline absorption, live retargeting, release on exit, Users derivation.
5. **Environment record.** Driver version and CUDA version from `nvidia-smi`; torch version from preflight; whether NVIDIA Control Panel exposes `CUDA - Sysmem Fallback Policy` and its value; which engine names Task Manager's `3D` dropdown offers (CUDA work appeared under `3D` on this host).
6. **README.md and GUIDE.md.** Both still say the gpu Dial reads NVML and that `tm` is display-only. Sections to reword: README.md Dial table (~line 80) and "What GPU utilization means on an A10-8Q slice" (~231-241); GUIDE.md tagline (~line 8), "How to read a line" (~466-494), "Now test the GPU" (~518-546), the troubleshooting row (~872) and "What is proven and what is not" (~920-938). The CSV column `gpu_tm` is now `gpu_nvml`; the console shows `(nvml n)` not `(tm n)`.
7. **Runbook step 0.** Rewrite to state the answer rather than the question, and drop the Compute_0 instruction: CUDA work shows under `3D` on this host.
