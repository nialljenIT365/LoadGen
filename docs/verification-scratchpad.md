# Verification scratchpad — host work list

The live to-do list for the A10 host. Work top to bottom. Paste output into
the `Result` block under each item exactly as it came, tick the box, then
commit and push from the host so the other side can pull and read it:

```
git pull
```

```
git add docs\verification-scratchpad.md
```

```
git -c user.name=nialljenIT365 commit -m "scratchpad: <item number>"
```

```
git push
```

Before any run: `git pull`, then `git log -1 --oneline`. The code must be at
`91850da` or later. Nothing else heavy on the host while a run is in
progress. Reference material: `docs\verification-runbook.md` for the reading
rules, `docs\verification-findings.md` for what is already settled.

Every command below is a single line. Paste one block at a time: PowerShell
5.1 swallows a multi-line paste as one input.

Cheap read-only items come first, then the idle Baseline, then the load runs.
The Baseline is the reference the load runs are judged against, so it has to
exist before them.

---

## 1. Pull

- [ ] Host is on `91850da` or later.

```
git pull
```

```
git log -1 --oneline
```

Result:

``` 
```

## 2. Environment record

For the findings. All read-only; changes nothing. Safe to run now.

- [ ] Driver, GPU name, frame buffer:

```
nvidia-smi --query-gpu=driver_version,name,memory.total --format=csv,noheader
```

- [ ] Torch and CUDA:

```
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available())"
```

- [ ] What is already in `analysis\` on the host:

```
Get-ChildItem analysis -Recurse -File | Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize
```

- [ ] Task Manager → Performance → GPU 0 → click the `3D` dropdown. List every
  engine name offered.
- [ ] NVIDIA Control Panel, if installed → Manage 3D Settings → is there a
  `CUDA - Sysmem Fallback Policy` entry, and what is it set to? If the Control
  Panel is not installed, say so.

Result:

```
```

## 3. Step 1 Baseline

Idle box, LoadGen not running, nothing else heavy. Two minutes, 24 samples,
aggregated by the one-liner. This is the reference every later release check
is read against.

- [ ] Done.

```
$rows = foreach ($i in 1..24) { nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits; Start-Sleep 5 }; $u = $rows | ForEach-Object { [int]($_ -split ',')[0] }; $m = $rows | ForEach-Object { [int]($_ -split ',')[1] }; "util.gpu  min $(($u | Measure-Object -Minimum).Minimum)  max $(($u | Measure-Object -Maximum).Maximum)  mean $([math]::Round(($u | Measure-Object -Average).Average,1))  nonzero $(($u | Where-Object { $_ -gt 0 }).Count)/24"; "mem.used  min $(($m | Measure-Object -Minimum).Minimum)  max $(($m | Measure-Object -Maximum).Maximum) MiB"
```

While that runs, note the Task Manager idle range over the same two minutes:
CPU %, Memory in use (GB and %), GPU 0 %, Dedicated GPU memory, Shared GPU
memory. A low and a high figure for each is enough.

Shared GPU memory also answers a loose end: the first `--vram-fill 95` run
spilled 88 GB into shared memory, and this confirms the box released it when
that process was killed.

Save one Task Manager screenshot as
`analysis\evidence\run-2026-09-09-<time>-baseline.png`.

Result:

```
```

## 4. gpu 40 closed-loop run

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
item 3 Baseline on exit.

Result:

```
```

---

Items 1 to 4 are done. What they showed changed items 5 and 6 from the plan
that was here before, and settled two things that no longer need testing:

- Item 4's startup warning was an artifact, not a fault. Fixed: the Dial now
  allows the counter one tick to publish its first sample before warning.
- The CSV now logs `gpu_duty`. Without it a log cannot tell a Dial that
  corrected from one that was seeded correctly and never moved.
- The `analysis\` directory on the host is empty, so no CSV from the earlier
  70/70/70 run survives. That item is dropped.

## 5. Retargeting — the convergence test

Item 4 could not test convergence and neither can any cold start. The seed is
`Target/100` and the counter reads about 0.9 × duty, so a fresh run always
begins inside ±10 of Target. `converge 0s` was arithmetic, not a result.
Convergence is only visible when the Target moves under a running loop.

Target 80 also puts the load clearly above this box's Baseline, which is 12 to
17% with spikes to 39% because of the RDP session. Target 40 sat inside that
noise.

- [ ] Two PowerShell windows. In the first, start the run. Change `1810` to
  the launch time.

```
python loadgen.py --gpu 80 --duration 6m --log analysis\run-2026-09-09-1810-gpu80-retarget.csv
```

- [ ] At about 2 minutes, in the second window, drop the Target to 40:

```
python -c "import json; p=r'C:\Tools\LoadGen\targets.json'; d=json.load(open(p)); d['gpu']=40; json.dump(d, open(p,'w'), indent=2); print('gpu ->', d['gpu'])"
```

- [ ] At about 4 minutes, put it back to 80:

```
python -c "import json; p=r'C:\Tools\LoadGen\targets.json'; d=json.load(open(p)); d['gpu']=80; json.dump(d, open(p,'w'), indent=2); print('gpu ->', d['gpu'])"
```

Capture:

- the console lines either side of each change, about four before and eight
  after, so the climb and the fall are both visible
- Task Manager GPU 0 Utilization, the figure at the bottom of the GPU pane,
  once while Target is 80 and once while it is 40
- any line containing `warning:`. There should now be none.
- the `released:` line, and GPU 0 Utilization within 5 s of exit

Then:

```
python analyse_csv.py analysis\run-2026-09-09-1810-gpu80-retarget.csv
```

Pass: each new Target takes effect within 2 ticks, the Actual reaches the new
Target within 20 s in both directions, and `gpu_duty` visibly moves rather
than sitting at its seed. A Target of 80 reading 72 or so is the 0.9 slope,
not a failure.

Result:

```
```

## 6. vram Actual against its Consumer

Item 4 printed `vram 0/ 29` while Task Manager showed Dedicated GPU memory at
1.4 to 1.5 of 7.5 GB. Both can be true: the vram Actual is NVML `used/total`,
which counts LoadGen's own CUDA context, and Task Manager's Dedicated figure
undercounts against NVML. This reads the two at the same moment instead of
reasoning about it.

- [ ] Two windows. First window, a bare run with every Dial at 0 — it takes no
  load, it only prints Actuals:

```
python loadgen.py --gpu 0 --duration 90s
```

- [ ] Second window, while that runs:

```
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits -l 5
```

Capture five console lines from the first window and the five `nvidia-smi`
lines closest to them in time, plus Task Manager Dedicated GPU memory at the
same moment.

Pass: the printed vram Actual equals `memory.used / memory.total`, within a
point. If it does, the Dedicated figure is the one that disagrees and the
findings say so; the vram Dial is sound either way.

Result:

```
```

---

## Queued behind items 5 and 6

- Baseline absorption, runbook step 4. Needs a Target above the Baseline held
  while artificial background load is added and removed.
- Release on exit under Ctrl+C during a large RAM fill, runbook step 6.
- The Users derivation, runbook step 7: `--users 15` for 2 minutes.
- All four Dials at 70, five minutes, once the gpu loop is settled.
