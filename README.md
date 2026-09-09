# NV SKU GPU Testing — synthetic load generator

`loadgen.py` holds GPU, VRAM, CPU and RAM on an Azure Virtual Desktop session
host (NV12ads_A10_v5) at chosen percentages, so user density on the GPU SKU can
be tested without real users. One real human session can sit alongside the
synthetic load to judge responsiveness.

`profiler.py` is its companion: run inside one real user's session, it measures
what that session costs and writes the User Profile that `--users` multiplies.

Read [CONTEXT.md](CONTEXT.md) for the vocabulary — Dial, Target, Actual,
Baseline, Self-check, Users, User Profile, Consumer, Reference Session,
Profiler — and the two decisions behind the design:
[ADR-0001](docs/adr/0001-target-is-consumer-figure.md) on what a Target means,
and [ADR-0002](docs/adr/0002-profiler-measures-own-session.md) on how the
Profiler attributes cost to a session.

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
**guess**. Measure one real user with the Profiler and pass the result with
`--profile`. Every density figure the tests produce is only as good as that
profile.

## The Profiler

`profiler.py` measures one **Reference Session** — one real person, logged into
the host, doing representative work from [CHECKLIST.md](CHECKLIST.md) — and
writes a User Profile.

```
python profiler.py --duration 20m
```

Run by that person, as themselves, from the same folder and venv. A standard
account is enough: it needs no administrator rights and writes nothing under
the script's own folder. Output goes to `%USERPROFILE%\Documents\LoadGen\`,
created if absent, as a matching `.json` and `.csv` pair named
`profile-<user>-<YYYY-MM-DD-HHMM>`.

| Flag | Default | What it does |
|---|---|---|
| `--duration` | until Ctrl+C | Run time, `s`/`m`/`h` suffix |
| `--warmup N` | 60 | Seconds of samples discarded at the start |
| `--checklist NAME` | read from `CHECKLIST.md` | Checklist name recorded in the output |
| `--out DIR` | `%USERPROFILE%\Documents\LoadGen` | Output folder |
| `--tick N` | 2 | Seconds between samples |

**How it attributes cost.** Every 2 seconds it re-enumerates the processes
sharing its own Windows session ID, excluding itself, and sums them: CPU from
`cpu_percent` divided by the core count so 100 means the whole host, RAM from
`rss`, GPU and VRAM per process from NVML with the Windows GPU Engine and GPU
Process Memory counters as fallback. It measures its own session from the
inside rather than subtracting a Baseline from whole-host figures, which is why
it needs no admin rights and no separate Baseline capture —
[ADR-0002](docs/adr/0002-profiler-measures-own-session.md) has the reasoning and
the cost: SYSTEM-owned per-session processes a standard user cannot read are
skipped, and `meta.skipped_processes` counts them.

**How the four numbers are chosen.** After the warm-up is discarded, `gpu` and
`cpu` take the mean and `vram_mb` and `ram_gb` take the p95: a user is not at
peak compute all day, but memory once allocated stays allocated. Fewer than 30
retained samples still writes the file and sets `meta.too_short`.

Console, one line per tick, `--` for anything unavailable:

```
10:15:02  procs  23 | cpu 5.8 | ram 2.4 GB | gpu 3.9 | vram 310 MB
```

The `.json` carries `per_user` (what LoadGen reads), `stats` (mean, p95 and
peak per metric) and `meta` — host, user, session ID, sample counts, the
checklist name and version, which GPU method was used, and the whole-host
NVML-to-GPU-Engine ratio so any scale mismatch between the two Consumers is
visible. The `.csv` has one row per retained tick:
`timestamp, procs, cpu, ram_gb, gpu, vram_mb, host_gpu_nvml, host_gpu_engine`.

Then feed it to LoadGen:

```
python loadgen.py --profile "C:\Users\jdoe\Documents\LoadGen\profile-jdoe-2026-09-09-1015.json" --users 15
```

`--profile` takes exactly one file and reads only its `per_user` block, which
replaces the shipped defaults when `targets.json` is written at launch. A file
with no usable `per_user` is rejected with a one-line error. Nothing else about
LoadGen changes.

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

For the Profiler, on the same machine: session process counting (210 processes
in session 1, 0 skipped), cpu and ram_gb tracking a known load (three busy
processes on 12 logical CPUs read 26.3% of the host), `--warmup 0 --duration
30s` retaining 16 samples and setting `too_short`, Ctrl+Break mid-run writing
both files and exiting in 0.33 s, the output folder being created when absent,
nothing written under the script's own folder, and `--profile` putting a
profile's `per_user` block into `targets.json` — including the rejection path
for a file with no `per_user`.

**Not verified here — the operator confirms these on the AVD host:** the GPU
Dial moving nvidia-smi and Task Manager, the VRAM Dial reaching a given
`memory.used`, and `--users 15` populating all four Dials. For the Profiler,
every GPU and VRAM path is unverified: NVML per-process attribution, the GPU
Engine fallback, which method gets chosen, whether `vram_mb` is plausible for a
desktop session, and the NVML-to-GPU-Engine ratio. On this machine NVML is
absent, so those columns were exercised only in their null state.

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

Points where the Profiler brief left room:

- **A null `per_user` value falls back to the shipped default rather than
  failing.** `--profile` rejects a file with no usable `per_user` at all, but a
  profile taken where NVML was unavailable has `gpu` and `vram_mb` as `null`;
  those two fall back to the built-in guesses with a warning naming them. The
  measured `cpu` and `ram_gb` are still worth having, and refusing the file
  would throw them away.
- **Both GPU sources are sampled every tick and the decided method selects
  which series is reported.** The brief says to decide the method once and not
  flip mid-run. Recording both and choosing at the end gives exactly that, and
  avoids the alternative where samples taken before the decision came from a
  different source than the ones after it.
- **The GPU Engine fallback is only reachable when NVML is up.** The fallback
  exists for a vGPU driver that will not attribute per process, not for a
  machine with no NVIDIA GPU. With NVML absent, `gpu_available` is `false`,
  `gpu` and `vram_mb` are null, and the PDH counters are not started at all —
  otherwise a machine with an Intel GPU would report GPU figures in a profile
  that also declares the GPU unavailable.
- **The whole-host GPU Engine figure sums every instance**, per the Profiler
  brief, where loadgen's display-only `tm` figure takes the busiest engine
  type. The two are not comparable and are not meant to be: this one exists
  only to form `meta.nvml_to_engine_ratio`.
- **The per-process GPU Engine figure also sums across engine types** for a
  pid, as the brief specifies. On a machine using several engines at once this
  can exceed 100 for one process.
- **p95 is nearest-rank, not interpolated**, so every reported figure is a
  value that was actually observed.
- **`--out` defaults to `%USERPROFILE%\Documents\LoadGen`** literally, as the
  brief states. Where Documents is redirected to OneDrive or a FSLogix
  container, the known-folder path is not resolved and the files land in the
  local path; `--out` overrides it.
- **`meta.skipped_processes` counts distinct processes, not tick events.** A
  process that refuses to answer on every one of 600 ticks is counted once, so
  the number reads as "four processes were missed" rather than a tick tally.
- **A newly started process contributes 0 to `cpu` on its first tick.**
  `cpu_percent` measures the interval since the previous call on that process
  object, and there is no previous call yet. With a 2-second tick over a
  20-minute run this is noise, and the warm-up covers the sign-in burst.
- **A running `loadgen.py` warns and continues**, per the brief. The warning
  names the consequence — its synthetic load would be measured as part of the
  session — but the operator decides whether to stop.

## Out of scope

Multiple fake user sessions or automated logons; Nerdio and Azure Monitor
counter parity; disk and network load; any host-level GPU or clock control.

For the Profiler: driving the workload (a person follows `CHECKLIST.md` by
hand); merging several profiles, since `--profile` takes exactly one file; any
Baseline capture; and measuring any session other than its own.
