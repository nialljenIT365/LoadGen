# Verification scratchpad — host work list

The live to-do list for the A10 host. Work top to bottom. Paste output into
the `Result` block under each item exactly as it came, tick the box, then
commit and push from the host so the other side can pull and read it:

```
git pull
git add docs\verification-scratchpad.md
git -c user.name=nialljenIT365 commit -m "scratchpad: <item number>"
git push
```

Before any run: `git pull`, then `git log -1 --oneline`. The code must be at
`6d5869b` or later. Nothing else heavy on the host while a run is in
progress. Reference material: `docs\verification-runbook.md` for the reading
rules, `docs\verification-findings.md` for what is already settled.

---

## 1. Pull

- [ ] Host is on `6d5869b` or later.

```
git pull
git log -1 --oneline
```

Result:

```
```

## 2. gpu 40 closed-loop run

First execution of `1ce4f25` on real hardware: the gpu Actual is now Task
Manager's GPU Engine counter. Target 40 is the case that used to burn the
whole vGPU while printing 0.

- [ ] Run for 3 minutes. Change `1530` to the launch time.

```
python loadgen.py --gpu 40 --duration 3m --log analysis\run-2026-09-09-1530-gpu40.csv
```

Capture while it runs:

- the first five console lines after `gpu: load thread started`
- any line containing `warning:`
- Task Manager → Performance → GPU 0 percentage at about 60 s and 120 s
- the `duration reached` and `released:` lines
- GPU 0 percentage within 5 s of exit

Then:

```
python analyse_csv.py analysis\run-2026-09-09-1530-gpu40.csv
```

Pass: converges within 20 s, steady state within ±10 of 40, releases to the
Baseline on exit.

Result:

```
```

## 3. All four Dials at 70

The GUIDE.md quick-start command. Covers vram, cpu and ram convergence in one
run, plus gpu at 70 on the new loop.

- [ ] Run for 5 minutes. Change `1615` to the launch time.

```
python loadgen.py --gpu 70 --vram 70 --cpu 70 --ram 70 --duration 5m --log analysis\run-2026-09-09-1615-gpu70-vram70-cpu70-ram70.csv
```

Capture:

- any line containing `warning:`
- the `released:` line
- within 5 s of exit, Task Manager: CPU %, Memory in use (GB), GPU 0 %,
  Dedicated GPU memory, Shared GPU memory

Then:

```
python analyse_csv.py analysis\run-2026-09-09-1615-gpu70-vram70-cpu70-ram70.csv
```

Result:

```
```

## 4. Step 1 Baseline

Idle box, LoadGen not running. Two minutes, 24 samples, aggregated. Paste
the whole block into PowerShell.

- [ ] Done.

```
$rows = foreach ($i in 1..24) { nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits; Start-Sleep 5 }
$u = $rows | ForEach-Object { [int]($_ -split ',')[0] }
$m = $rows | ForEach-Object { [int]($_ -split ',')[1] }
"util.gpu  min $(($u | Measure-Object -Minimum).Minimum)  max $(($u | Measure-Object -Maximum).Maximum)  mean $([math]::Round(($u | Measure-Object -Average).Average,1))  nonzero $(($u | Where-Object { $_ -gt 0 }).Count)/24"
"mem.used  min $(($m | Measure-Object -Minimum).Minimum)  max $(($m | Measure-Object -Maximum).Maximum) MiB"
```

While it runs, note the Task Manager idle range over the same two minutes:
CPU %, Memory in use (GB and %), GPU 0 Dedicated GPU memory, Shared GPU
memory. Save one Task Manager screenshot as
`analysis\evidence\run-2026-09-09-<time>-baseline.png`.

Result:

```
```

## 5. Environment record

For the findings. All read-only; change nothing.

- [ ] Driver line:

```
nvidia-smi --query-gpu=driver_version,name,memory.total --format=csv,noheader
```

- [ ] Task Manager → Performance → GPU 0 → click the `3D` dropdown. List
  every engine name offered.
- [ ] NVIDIA Control Panel, if installed → Manage 3D Settings → is there a
  `CUDA - Sysmem Fallback Policy` entry, and what is it set to?
- [ ] Files on the host:

```
Get-ChildItem analysis -Recurse -File | Select-Object FullName, Length, LastWriteTime
```

Result:

```
```

## 6. Earlier 70/70/70 run, if it ran

The run launched before `1ce4f25`. Its gpu column is NVML and is not
evidence; its cpu and ram columns are, and its `gpu_tm` column shows the
engine counter under the old bouncing duty.

- [ ] If a CSV exists from that run:

```
python analyse_csv.py analysis\run-2026-09-09-1615-gpu70-cpu70-ram70.csv
```

Result:

```
```
