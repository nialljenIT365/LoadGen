# profiler build brief

Build brief for the Profiler, the companion to loadgen. Read `CONTEXT.md` for vocabulary (Reference Session, Profiler, User Profile, Users, Baseline) and `docs/adr/0002-profiler-measures-own-session.md` for the one non-obvious decision. Settled in a grilling session on 2026-09-09; not open for re-litigation except the items at the end.

## Purpose

Replace GUIDE.md Step 8's manual measurement. A real person logs into the session host, starts the Profiler, works through `CHECKLIST.md`, stops the Profiler. The Profiler writes a User Profile file that loadgen can load with `--profile`.

## Who runs it and where

- Run by the Reference Session user, a standard (non-admin) account, from `C:\Tools\LoadGen\` using the existing venv read-only. Must not need to write anywhere under `C:\Tools\`.
- Output goes to `%USERPROFILE%\Documents\LoadGen\`. Create the folder if absent.
- Host is otherwise idle. loadgen must not be running; if a `loadgen.py` process is visible, print a warning and continue.

## Deliverables

1. `profiler.py` at repo root. Python 3.12, stdlib plus `psutil` and `nvidia-ml-py`. No torch. Performance counters read through PDH via `ctypes` (no pywin32). Must run with GPU support absent (NVML import fails, or no NVIDIA device): GPU and VRAM columns become null and meta says why.
2. `CHECKLIST.md` at repo root: the app checklist the reference user follows. Seed it with a generic office workload (sign in, open browser with five tabs, open a document and edit for five minutes, play a video for two minutes, video call or equivalent if available, idle two minutes) with rough timings adding up to about 20 minutes. Top of file carries a `name` and `version` line the Profiler records.
3. `loadgen.py` change: new `--profile PATH`. At launch, read the file, take its `per_user` block, and use it in place of `DEFAULT_PER_USER` when writing `targets.json`. Reject files without a valid `per_user`. Do not change any other loadgen behaviour.
4. `GUIDE.md` Step 8 rewritten: run the Profiler instead of reading Task Manager by hand. Keep the warning that the shipped defaults are guesses.
5. `README.md` gains a short Profiler section and the `--profile` flag.
6. `requirements.txt` unchanged unless a new dependency is unavoidable.

## Measurement

Attribution is per session, from inside (ADR-0002).

- Session ID: `ProcessIdToSessionId` on the Profiler's own pid via `ctypes` (kernel32).
- Process set: every process whose session ID matches, excluding the Profiler's own pid. Re-enumerate every tick because apps start and stop. Processes that raise AccessDenied are skipped and counted in `meta.skipped_processes`.
- Tick every 2 s.

Per tick, for the process set:

| Metric | Source | Unit in sample |
|---|---|---|
| cpu | sum of `psutil.Process.cpu_percent()` across the set, divided by `os.cpu_count()` so 100 means the whole host | percent of host |
| ram | sum of `memory_info().rss` | GB |
| gpu | NVML `nvmlDeviceGetProcessUtilization` filtered to the set, summing `smUtil`; if that call raises NotSupported or returns nothing for a busy set, fall back to the `\GPU Engine(*)\Utilization Percentage` counter, summing instances whose name contains `pid_<pid>` for pids in the set | percent of vGPU |
| vram | NVML `nvmlDeviceGetGraphicsRunningProcesses` plus `ComputeRunningProcesses` filtered to the set, summing `usedGpuMemory`; fallback `\GPU Process Memory(*)\Dedicated Usage` for the same pids | MB |

Also sample every tick, whole host, for the cross-check only: NVML `utilization.gpu` and the total of `\GPU Engine(*)\Utilization Percentage`. Record their mean ratio in `meta.nvml_to_engine_ratio`. Do not use it to scale anything.

Decide the GPU method once, on the first tick where the set has any process with GPU activity, and record it in `meta.method.gpu` and `meta.method.vram`. Do not flip methods mid-run.

## Warm-up and statistics

- Discard the first 60 s of samples (`--warmup 60`, seconds, 0 allowed).
- After discard, compute per metric: mean, p95, peak.
- `per_user` values: `gpu` = mean, `cpu` = mean, `ram_gb` = p95, `vram_mb` = p95. Round to one decimal.
- Fewer than 30 retained samples: still write the file, set `meta.too_short = true`, print a warning.

## Control and lifecycle

- CLI: `--duration 20m` optional (s/m/h suffix), else run until Ctrl+C. `--warmup 60`. `--checklist NAME` free text recorded in meta, default read from `CHECKLIST.md` header if found next to the script. `--out DIR` overrides the Documents folder. `--tick 2`.
- Console: one line per tick, `HH:MM:SS  procs 23 | cpu 5.8 | ram 2.4 GB | gpu 3.9 | vram 310 MB`, with `--` for unavailable metrics. Print a final summary table (mean, p95, peak per metric) and the output path.
- Exit within 2 s of Ctrl+C. Always write the output files if at least one retained sample exists.

## Output

Two files, same stem: `profile-<username>-<YYYY-MM-DD-HHMM>.json` and `.csv`.

CSV: one row per retained tick, columns `timestamp, procs, cpu, ram_gb, gpu, vram_mb, host_gpu_nvml, host_gpu_engine`.

JSON. The `host`, `user`, timestamp and every figure below are **invented
examples** used to show the shape of the file — they are not from a real host,
account or run:

```json
{
  "per_user": { "gpu": 3.9, "cpu": 5.8, "vram_mb": 310, "ram_gb": 2.6 },
  "stats": {
    "gpu":     { "mean": 3.9, "p95": 9.1, "peak": 22.0 },
    "cpu":     { "mean": 5.8, "p95": 14.2, "peak": 31.0 },
    "vram_mb": { "mean": 280, "p95": 310, "peak": 340 },
    "ram_gb":  { "mean": 2.3, "p95": 2.6, "peak": 2.8 }
  },
  "meta": {
    "host": "AVD-NV12-01",
    "user": "jdoe",
    "session_id": 3,
    "started": "2026-09-09T10:15:00+04:00",
    "duration_s": 1210,
    "tick_s": 2,
    "warmup_s": 60,
    "samples_retained": 575,
    "skipped_processes": 4,
    "checklist": "office-generic v1",
    "method": { "gpu": "nvml", "vram": "nvml" },
    "nvml_to_engine_ratio": 1.12,
    "too_short": false,
    "gpu_available": true
  }
}
```

`per_user` keys and units must match what `loadgen.py` already reads (`gpu` %, `cpu` %, `vram_mb` MB, `ram_gb` GB).

## Verification

Dev machine has no NVIDIA GPU. Verify here:

- Profiler runs, GPU and VRAM columns show `--`, JSON has `gpu_available: false` and null gpu/vram in `per_user`.
- Open a browser and a busy process during a 3 minute run; cpu and ram_gb in the summary are non-zero and plausible.
- `--warmup 0 --duration 30s` retains about 15 samples and sets `too_short`.
- Ctrl+C mid-run writes both files.
- `loadgen.py --profile <file> --users 10` writes `targets.json` with the file's `per_user` block. A file with no `per_user` is rejected with a clear message.
- Output folder created when absent. Nothing written under the script's own folder.

Verify on the AVD host only (operator): GPU method chosen and recorded, vram_mb plausible for a desktop session, ratio recorded.

## Out of scope

- Driving the workload. The person follows `CHECKLIST.md` by hand.
- Merging several profiles. `--profile` takes exactly one file.
- Any Baseline capture.
- Measuring other sessions.

## Open items for the operator, not the builder

- Replace the generic checklist with the real app set.
- Confirm on the host whether NVML per-process works on the A10-8Q profile; the meta will say.
