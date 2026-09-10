# Next steps — commands to run, files to change

Two parts. Part A runs on the A10 host. Part B is edits to this repo, made on
the workstation after Part A comes back.

Host repo: `C:\Tools\LoadGen`. Every command is one line — paste one block at a
time, PowerShell 5.1 swallows a multi-line paste. Captures go in
`analysis\evidence\`, never `docs\`.

---

# Part A — host runs

## A1. Pull

```
git pull
```

```
git log -1 --oneline
```

**Pass:** `85ff5a7` or later.

---

## A2. Retarget run — GPU 80 → 40 → 80

Needs two PowerShell windows, both at `C:\Tools\LoadGen`.

**Window 1**, start the run:

```
python loadgen.py --gpu 80 --duration 6m --log analysis\run-2026-09-10-1030-gpu80-retarget.csv
```

**Window 2**, at about 2 minutes elapsed:

```
python -c "import json; p=r'C:\Tools\LoadGen\targets.json'; d=json.load(open(p)); d['gpu']=40; json.dump(d, open(p,'w'), indent=2); print('gpu ->', d['gpu'])"
```

**Window 2**, at about 4 minutes elapsed:

```
python -c "import json; p=r'C:\Tools\LoadGen\targets.json'; d=json.load(open(p)); d['gpu']=80; json.dump(d, open(p,'w'), indent=2); print('gpu ->', d['gpu'])"
```

**Window 1**, after it exits:

```
python analyse_csv.py analysis\run-2026-09-10-1030-gpu80-retarget.csv
```

Capture:

1. 4 console lines before the 80→40 change and 8 after.
2. 4 console lines before the 40→80 change and 8 after.
3. Task Manager → Performance → GPU 0 → the `Utilization` figure at the bottom
   of the pane. Once while Target is 80, once while Target is 40.
4. Every line containing `warning:`.
5. The `released:` line, and `Utilization` within 5 s of exit.
6. The full `analyse_csv.py` output.

Pass:

- No `warning:` line. One appearing means the counter genuinely failed.
- Each new Target takes effect within 2 ticks, so 4 seconds.
- The Actual reaches the new Target within 20 s, both directions.
- Target 80 reads about 72. That is the 0.9 counter slope, not a miss.

---

## A3. VRAM Actual against nvidia-smi

Two windows.

**Window 1**, a run that takes no load and only prints Actuals:

```
python loadgen.py --gpu 0 --duration 90s
```

**Window 2**, while that runs:

```
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits -l 5
```

Capture 5 console lines from Window 1, the 5 `nvidia-smi` lines nearest them in
time, and Task Manager `Dedicated GPU memory` at the same moment.

**Pass:** LoadGen's `vram` Actual equals `memory.used / memory.total × 100`
within 1 point.

---

## A4. Baseline absorption

Tests the ADR-0001 claim: a Target is the figure the monitoring tool shows, not
an amount added on top.

**Window 1:**

```
python loadgen.py --cpu 70 --duration 8m --log analysis\run-2026-09-10-1100-cpu70-absorb.csv
```

**Window 2**, at about 2 minutes, add unrelated CPU load:

```
python -c "import multiprocessing as mp, time; ps=[mp.Process(target=lambda: [x*x for x in iter(int,1)]) for _ in range(4)]; [p.start() for p in ps]; time.sleep(180); [p.terminate() for p in ps]; print('background load done')"
```

That runs 4 busy processes for 3 minutes then stops on its own.

Capture:

- Task Manager CPU % at 1 min (LoadGen alone), 3 min (LoadGen plus background),
  and 7 min (LoadGen alone again).
- Console lines at those three moments.
- The `warning:` line if one appears.

**Pass:** Task Manager CPU stays near 70 at all three moments. It must not
climb toward 100 when the background load is added.

Then the below-Baseline case. **Window 2**, while A4 is still running:

```
python -c "import json; p=r'C:\Tools\LoadGen\targets.json'; d=json.load(open(p)); d['cpu']=5; json.dump(d, open(p,'w'), indent=2); print('cpu ->', d['cpu'])"
```

**Pass:** one warning naming the Target, the Baseline, and that it is
contributing nothing. It must print once, not every tick.

---

## A5. Release on exit

**Window 1:**

```
python loadgen.py --gpu 60 --vram 50 --cpu 60 --ram 70
```

Let it reach steady state, then press Ctrl+C.

**Window 2**, within 5 seconds of the Ctrl+C:

```
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits
```

```
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, WorkingSet
```

Capture the `released:` line, both command outputs, and Task Manager CPU,
Memory, GPU 0.

Repeat once, pressing Ctrl+C while the RAM fill is still climbing rather than
at steady state.

Pass:

- `released:` line appears.
- `nvidia-smi` back to the A3 idle figures.
- No `python.exe` left in `Get-Process`.
- All within 5 seconds.

---

## A6. Users derivation

```
python loadgen.py --users 15 --duration 2m --log analysis\run-2026-09-10-1130-users15.csv
```

Capture the first console line, which shows the derived Targets.

**Pass:** gpu 60, cpu 90, vram about 55, ram about 34.

---

## A7. All four Dials

```
python loadgen.py --gpu 70 --vram 70 --cpu 70 --ram 70 --duration 5m --log analysis\run-2026-09-10-1140-all70.csv
```

```
python analyse_csv.py analysis\run-2026-09-10-1140-all70.csv
```

Capture every `warning:` line, the `released:` line, and within 5 s of exit:
Task Manager CPU %, Memory GB, GPU 0 %, Dedicated GPU memory, Shared GPU
memory.

Pass, per `docs\verification-runbook.md`: cpu within ±5, ram within ±2, gpu
within ±10, vram within ±3.

---

# Part B — repo edits

Made on the workstation, after Part A. Line numbers are as at `d6be9c8`.

## B1. README.md

| Line | Now | Change to |
|---|---|---|
| 80 | `gpu` Actual read from ``NVML `nvmlDeviceGetUtilizationRates().gpu` `` | Windows `\GPU Engine(*)\Utilization Percentage` via PDH, busiest engine type |
| 210 | sample line shows `(tm  55)` | `(nvml  55)` — the console column was renamed |
| 213-214 | "`tm` is the Windows GPU Engine counter… It is display-only; the Self-check chases NVML." | `nvml` is NVML utilisation and is display-only; the Self-check chases the GPU Engine counter |
| 233 | "NVML utilization inside the…" time-slicing explanation | Replace with the measured result: NVML does not track this guest's work at all, 0 to 100 under constant load |
| 239-240 | "can differ from NVML by 10 to 20 points. The script chases NVML" | The two measure different things, not a fixed offset. The script chases the engine counter |
| 265-267 | "every GPU and VRAM path is unverified" | Narrow to what is still unverified after this round |

## B2. GUIDE.md

| Line | Now | Change to |
|---|---|---|
| 463-469, 477, 611-618 | sample output shows `(tm  N)` | `(nvml  N)` throughout |
| 494-496 | "`tm` is Task Manager's GPU figure… shown for information only — LoadGen steers by the NVML number (the `gpu` Actual), not by `tm`. The two routinely differ by 10 to 20 points." | Invert: the `gpu` Actual **is** Task Manager's figure; `nvml` is the display-only column. Drop the 10-to-20-point claim |
| 407 | `nvidia-ml-py` "reads the GPU through NVML, the same interface" | Keep — NVML still supplies the VRAM Actual. Add that GPU utilisation does not come from it |
| 801-813 | `gpu`/`vram` show `--` troubleshooting | Check the `gpu` case still describes the engine counter, not NVML |
| 880 | "`gpu` Actual stays `--`… NVML cannot read utilisation" and "This path is unverified on real hardware" | Cause is the GPU Engine counter, not NVML. Remove "unverified" — A2 covers it |
| 881 | "`tm` differs from the `gpu` Actual by 10–20 points… LoadGen steers by the NVML figure." | Delete the row or invert it |
| 943 | "the NVML-to-GPU-Engine ratio" in the unverified list | Update to what remains unverified |

## B3. docs\verification-runbook.md

| Line | Change |
|---|---|
| 40 | Heading `## Step 0 — is the gpu Actual trustworthy` → state the answer, not the question |
| 73 | "set one Task Manager GPU graph to Compute_0 (or Cuda)" → this host offers only `3D`, `Copy`, `Video Encode`, `Video Decode`. CUDA work appears under `3D`. Delete the Compute_0 instruction |
| 116 | Already correct — leave |

## B4. docs\verification-findings.md

Replace the `## Pending` section with results. One subsection per Dial, each
answering four questions:

1. Does the printed Actual agree with an independent Consumer?
2. Does it reach the Target?
3. How accurately does it hold?
4. Does it release on exit?

Then sections for Baseline absorption (A4), live retargeting (A2), release
(A5), the Users derivation (A6), and the environment record.

Environment record, already captured:

```
Driver           574.24 (NVML) / 32.0.15.7424 (WDDM), dated 7/6/2026
GPU              NVIDIA A10-8Q, 8192 MiB
torch            2.14.0+cu126, CUDA 12.6, torch.cuda.is_available() True
Engine types     3D, Copy, Video Encode, Video Decode. No Compute_0.
Host             12 logical CPUs, 110 GB RAM
Idle Baseline    CPU 11-29%, GPU Utilization 12-17% with spikes to 39%,
                 NVML util 0-12 mean 0.9, memory.used 1036-1150 MiB,
                 Shared GPU memory 0.1/88.0 GB
```

Idle is not idle here: the readings above are taken through an RDP session and
the session itself uses the machine. Test Targets need to sit clear of that,
which is why A2 uses 80 rather than 40.
