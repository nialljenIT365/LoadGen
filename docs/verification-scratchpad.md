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

## Queued behind item 4

Not started until item 4 comes back — what it shows changes whether these run
as written.

- **All four Dials at 70**, the GUIDE.md quick-start command, five minutes.
  Covers vram, cpu and ram convergence in one run.
- **The earlier 70/70/70 run**, if a CSV from it survives on the host. Its
  `gpu` column is NVML and is not evidence; its `cpu` and `ram` columns are.
- Runbook steps 4 to 7: Baseline absorption, live retargeting, release on
  exit, the Users derivation.
