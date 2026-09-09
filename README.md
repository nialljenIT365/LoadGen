# NV SKU GPU Testing — synthetic load generator

`loadgen.py` holds GPU, VRAM, CPU and RAM on an Azure Virtual Desktop session
host (NV12ads_A10_v5) at chosen percentages, so user density on the GPU SKU can
be tested without real users. One real human session can sit alongside the
synthetic load to judge responsiveness.

Read [CONTEXT.md](CONTEXT.md) for the vocabulary — Dial, Target, Actual,
Baseline, Self-check, Users, User Profile, Consumer — and
[docs/adr/0001-target-is-consumer-figure.md](docs/adr/0001-target-is-consumer-figure.md)
for the one non-obvious decision behind it.

**A Target is the overall figure a Consumer displays, not the script's own
footprint.** Every Self-check tick the script reads each Actual and contributes
only the gap between the Baseline and the Target. Ask for `cpu 60` and Task
Manager reads about 60, whatever the machine was already doing.

## Install (on the Windows host)

1. Install Python, then open a new shell so `py` is on PATH:

   ```
   winget install Python.Python.3.12
   ```

2. Run `nvidia-smi` and read **CUDA Version** in the header. 12.6 or newer uses
   the `cu126` torch index; anything older uses `cu124`. Both run on GRID R535
   and newer through CUDA minor-version compatibility, and the A10 is `sm_86`,
   for which torch ships native kernels.

   | CUDA Version in the header | Index URL |
   |---|---|
   | 12.6 or newer | `https://download.pytorch.org/whl/cu126` |
   | older | `https://download.pytorch.org/whl/cu124` |

3. Create and activate a virtual environment:

   ```
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   ```

4. Install torch from the chosen index, then the rest:

   ```
   pip install torch --index-url https://download.pytorch.org/whl/cu126
   pip install -r requirements.txt
   ```

   `requirements.txt` holds `psutil` and `nvidia-ml-py` only. The torch index
   URL is deliberately not pinned in that file — it depends on the host driver.

5. Launch:

   ```
   python loadgen.py --gpu 40 --vram 25 --cpu 60 --ram 70 --duration 30m --log run1.csv
   ```

6. Or drive every Dial from a user count:

   ```
   python loadgen.py --users 15
   ```

CPU and RAM only? torch and `nvidia-ml-py` are imported lazily. With `--gpu 0`
and `--vram 0` (the defaults) the script runs with `psutil` alone.

## The four Dials

Each Dial is a percentage from 0 to 100. **A Dial at 0 leaves that resource
alone**: no workers, no allocation, no correction.

| Dial | What it holds | Actual read from |
|---|---|---|
| `gpu` | vGPU compute utilization (kernel busy time) | NVML `nvmlDeviceGetUtilizationRates().gpu` |
| `vram` | share of the vGPU frame buffer allocated | NVML `nvmlDeviceGetMemoryInfo()` used / total |
| `cpu` | total CPU across all logical processors | `psutil.cpu_percent()` |
| `ram` | physical memory in use | `psutil.virtual_memory().percent` (Task Manager's "In use") |

`ram` and `vram` clamp at 95 unless `--unsafe` is passed. `gpu` and `cpu` may
reach 100.

A Target below the current Baseline cannot be met. The script contributes
nothing, prints one warning, and keeps running:

```
warning: ram target 40 is below the current Baseline (56); contributing nothing
```

## Changing a Dial mid-run

At launch the script writes `targets.json` (beside the script, or at
`--targets PATH`), overwriting any existing file, from the command line values.
**After that the file is the sole source of truth** — it is re-read every 2
seconds, so editing it retargets a running load without a restart.

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

- A **numeric** Dial value is manual and wins.
- A **null** Dial is derived from `users` × `per_user`.
- `users` absent or null makes every Dial manual, and a null Dial reads as 0.

Save invalid JSON and the script warns once and keeps the last good values, so
a half-typed edit never drops the load.

## How `--users` works

`--users N` sets the Users count and leaves every Dial you did not name on the
command line as `null`, so it is derived from the User Profile:

```
gpu  = users × per_user.gpu
cpu  = users × per_user.cpu
vram = users × per_user.vram_mb / total_vram_mb × 100
ram  = users × per_user.ram_gb  / total_ram_gb  × 100
```

Results clamp exactly like manual values. Mixing is fine:
`--users 15 --cpu 90` derives gpu, vram and ram from 15 users and pins cpu to 90.

The shipped User Profile — gpu 4, cpu 6, vram_mb 300, ram_gb 2.5 — is a
**guess**. Measure one real user on the host doing representative work, then
replace those four numbers in `targets.json`. Every density figure the tests
produce is only as good as that profile.

## Output

One console line per 2-second tick, target/actual per Dial:

```
12:34:56  users  15 | gpu  60/ 58 (tm  55) | vram  52/ 51 | cpu  90/ 88 | ram  35/ 35
```

`tm` is the Windows **GPU Engine** performance counter — Task Manager's figure.
It is display-only; the Self-check chases NVML. `users` appears only when set.
Unreadable values print as `--`. A large RAM fill appends
`ram fill 12/40 blocks` until it settles.

`--log path.csv` appends one row per tick:
`timestamp, users, gpu_t, gpu_a, gpu_tm, vram_t, vram_a, cpu_t, cpu_a, ram_t, ram_a`.
Off by default.

## Lifecycle

`--duration` takes an `s`/`m`/`h` suffix (`90s`, `30m`, `2h`). Without it the
script runs until Ctrl+C. On any exit — duration reached, Ctrl+C, or an
unhandled error — all duties go to 0, CPU workers are terminated and joined,
RAM and VRAM blocks are dropped (`empty_cache` after the VRAM release), NVML is
shut down, and a final line confirms the release. Exit completes within 5
seconds.

## What GPU utilization means on an A10-8Q slice

The physical A10 is time-sliced between three VMs. NVML utilization inside the
guest reports **the share of scheduling time this vGPU was running a kernel**,
not the share of the physical A10. When neighbours are busy your kernels take
longer in wall-clock time, but the percentage still reads against your own time
slices — so the figure describes your slice's saturation, not the card's.

Task Manager's GPU Engine percentage comes from the Windows display driver
scheduler and can differ from NVML by 10 to 20 points. The script chases NVML
and prints both, so the divergence is visible rather than surprising.

Neither figure is a per-user measure. That is what the User Profile is for.

## Verified / not verified

Verified on a development machine with no NVIDIA GPU and no torch, Python 3.14:
CPU dial convergence at 30/60/90, RAM fill and release, below-Baseline
clamping, invalid JSON handling, `--duration` and Ctrl+C clean exit with no
orphan processes, and both torch-absent paths (Dials at 0 runs; a non-zero GPU
Dial gives a one-line error).

**Not verified here — the operator confirms these on the AVD host:** the GPU
Dial moving nvidia-smi and Task Manager, the VRAM Dial reaching a given
`memory.used`, and `--users 15` populating all four Dials.

## Assumptions

Points where the brief left room, resolved the way a careful reading suggests:

- **RAM sizing uses `total − available`**, which is what
  `virtual_memory().percent` reports and what Task Manager labels "In use".
  psutil's separate `used` field differs slightly on Windows, and matching the
  Consumer's figure matters more than matching the field name.
- **The console pads every percentage to three columns**, so the values stay
  aligned when a Dial reaches 100. The brief's example line shows two-digit
  values unpadded; the columns are otherwise identical.
- **The GPU Engine counter is read through PDH** (`ctypes` against `pdh.dll`)
  **on a background thread**, sampling once a second, rather than shelling out
  to `typeperf` inside the tick. `typeperf` needs at least a second per sample
  and would stall the Self-check. Windows publishes one instance per process
  per engine, so instances are summed within an engine type and the busiest
  engine type is reported — this is how Task Manager derives its figure. Any
  failure yields `--`.
- **A missing GPU stack is fatal at launch but not mid-run.** Starting with a
  non-zero `gpu` or `vram` Dial and no torch exits immediately with a one-line
  error. Editing `targets.json` to raise a GPU Dial during a run only warns and
  holds that Dial at 0 — a long RAM or CPU test should not die because of a
  typo in a file.
- **The below-Baseline warning uses a 2-point slack** on `cpu` and `gpu`. Those
  two Baselines cannot be isolated from the script's own contribution directly,
  so the condition is inferred from duty having fallen to 0 while the Actual is
  still above the Target. `ram` and `vram` compute their Baseline exactly.
- **`--users N` with `--gpu 0 --vram 0`** does not require torch: both GPU Dials
  are explicitly manual and zero, so nothing derives a GPU load.
- **Block counts round to nearest** rather than truncate, which halves the
  worst-case sizing error (128 MB of RAM, 32 MB of VRAM).
- **CPU workers are spawned even when the `cpu` Dial is 0**, sitting idle at
  duty 0. `targets.json` is the sole source of truth and can raise the Dial at
  any moment; the workers have to already exist for that to take effect within
  a tick.
- **The first tick's `cpu` figure is not meaningful.** `psutil.cpu_percent`
  measures the interval since the previous call, and that interval covers
  process startup — including spawning the workers, which briefly pegs every
  core. It is correct from the second tick.
- **Ctrl+Break is handled like Ctrl+C**, taking the same release path. Ctrl+C
  is the documented way to stop a run; Ctrl+Break simply does not leave load
  behind either.

## Out of scope

Multiple fake user sessions or automated logons; Nerdio and Azure Monitor
counter parity; disk and network load; any host-level GPU or clock control.
