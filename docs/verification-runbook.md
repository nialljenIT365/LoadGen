# LoadGen verification — host runbook

Execute on the NV12ads_A10_v5 session host. `analysis\verification-brief.md`
holds the reasoning behind each step; this file holds the commands and the
rules for reading what comes back. The brief is not in the repo -- `analysis\`
is gitignored, so it stays on the machine that wrote it. Everything needed to
execute step 0 to step 7 is here.

Read `CONTEXT.md` for vocabulary before writing anything down. Use Dial,
Target, Actual, Baseline, Self-check, Users, User Profile, Consumer exactly.

## Preflight

The CUDA context fix is already committed and pushed as `81178ca`. Pull before
starting, or step 0 measures the old code.

```
cd C:\path\to\NVSkuGPUTesting
git pull
git log -1 --oneline          # expect 81178ca or later
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python -c "import pynvml; pynvml.nvmlInit(); print('nvml ok')"
nvidia-smi
```

`torch.cuda.is_available()` must print `True` and `nvidia-smi` must name the
A10-8Q. If either fails, stop and fix the environment; nothing below produces a
meaningful number without them.

Create the evidence directory if it is absent:

```
mkdir analysis\evidence
```

Everything written during this session stays in `analysis\`, which is
gitignored. Fixes to `loadgen.py`, `README.md` and `GUIDE.md` are committed as
normal: author `nialljenIT365`, no AI co-author trailer, pull before pushing.

## Step 0 — is the gpu Actual trustworthy

Answer this before measuring anything else on the gpu Dial.

```
python gpu_probe.py --seconds 30
python gpu_probe.py --seconds 60 --vram-fill 95
```

Run the second even if the first looks clean. It reproduces the condition of
the 14:47 capture, where the frame buffer was held at 95% at the moment the gpu
Actual fell to 0.

Do not arbitrate with `nvidia-smi`. It reads the same NVML counter through the
same driver, so a broken counter produces two agreeing wrong answers.
Throughput in TFLOP/s is the arbiter, because the driver cannot misreport work
actually completed.

Read the result:

| Probe output | Conclusion | Where the fix belongs |
|---|---|---|
| Throughput above ~1 TFLOP/s, NVML near 0 | Instrument is wrong, load is fine | How `correct()` sources feedback |
| Throughput collapses or decays partway | Load fails under sustained running | `GpuLoad._run`, and what changes with a full frame buffer |
| NVML returns nothing at all | Profile does not expose utilisation to the guest | Not a bug; `README.md` and `GUIDE.md` must stop claiming it |

An A10-8Q should manage tens of TFLOP/s at fp16. Anything above about 1 means
real compute is happening.

Record which of `clocks.sm`, `power.draw` and `temp` the probe could read. Many
vGPU profiles withhold all three from the guest. Where they are available they
are the cleanest corroboration that silicon is busy.

Then set one Task Manager GPU graph to Compute_0 (or Cuda) and record what it
reads during a `--gpu 100` run. The four default graphs are 3D, Copy, Video
Encode and Video Decode; CUDA work lands on none of them. This establishes
whether the PDH counter LoadGen already samples in the `tm` column is usable as
a feedback signal.

Carry the answer into every gpu run below. If the gpu Actual cannot be trusted,
say so in the findings and do not report convergence numbers derived from it.

### Why this is a correctness defect, not a display defect

`correct()` cannot distinguish a broken instrument from a genuinely idle GPU —
both arrive as `actual = 0` — so it drives duty to 1.0 either way. At a Target
of 100 that is invisible. At a Target of 40 it burns the whole GPU while
printing 0 and believing it is undershooting. Whatever step 0 concludes, the
findings need a position on this.

## Step 1 — Baseline

With LoadGen not running, hold for at least two minutes and record the range,
not a single reading.

```
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv -l 5
```

Record idle gpu, vram, cpu and ram from Task Manager alongside it. Every later
judgement is relative to these numbers.

## Step 2 — trust the instrument before the numbers

For each Dial, read LoadGen's Actual and a second Consumer at the same moment.

| Dial | LoadGen reads | Cross-check against |
|---|---|---|
| gpu | NVML utilization | `nvidia-smi --query-gpu=utilization.gpu --format=csv -l 2` |
| vram | NVML memory used/total | `nvidia-smi --query-gpu=memory.used --format=csv -l 2` |
| cpu | `psutil.cpu_percent` | Task Manager; Perfmon `\Processor Information(_Total)\% Processor Utility` |
| ram | `psutil.virtual_memory().percent` | Task Manager "In use" |

Disagreement here invalidates everything downstream. Resolve it before step 3.

The `tm` column is the Windows GPU Engine counter and is display-only; the
Self-check never chases it. NVML and `tm` are expected to differ by 10 to 20
points. Record that divergence as a finding, not a fault.

## Step 3 — one Dial at a time, then all four

```
python loadgen.py --cpu 70  --duration 5m  --log analysis\cpu70.csv
python loadgen.py --ram 70  --duration 5m  --log analysis\ram70.csv
python loadgen.py --gpu 50  --duration 5m  --log analysis\gpu50.csv
python loadgen.py --vram 50 --duration 5m  --log analysis\vram50.csv
python loadgen.py --gpu 40 --vram 25 --cpu 60 --ram 70 --duration 30m --log analysis\all.csv
```

Derive the numbers rather than eyeballing the console:

```
python analyse_csv.py analysis\cpu70.csv
python analyse_csv.py analysis\all.csv
```

The script discards the first tick, splits each Dial into runs of constant
Target so live retargeting is judged separately, and reports time to converge,
mean and standard deviation of `actual - target` after convergence, and every
excursion outside tolerance. It counts blank readings apart from hard zeros: a
blank means the instrument failed, a `0.0` means NVML answered and answered
zero. That distinction is the step 0 signature and it will show up here too.

Tolerances the script applies:

| Dial | Converges within | Steady-state |
|---|---|---|
| cpu | 10 s | +/-5 |
| ram | 10 s plus fill time | +/-2 |
| gpu | 20 s | +/-10 |
| vram | 1 tick | +/-3 |

On `--vram 50` against an 8 GB frame buffer, `nvidia-smi` should show
`memory.used` near 4096 MiB. Note MiB against MB when comparing figures.

## Step 4 — Baseline absorption

This is the ADR-0001 test and it is the one that decides whether the design
claim holds.

1. Record the idle Baseline for the Dial.
2. Raise it deliberately — open applications, or run a second load — and record
   the new Baseline.
3. Start LoadGen with a Target comfortably above the raised Baseline.
4. Confirm the Consumer settles on the Target, not on Baseline plus a fixed
   contribution.
5. While LoadGen holds, remove the artificial load. Confirm the Consumer stays
   on the Target as LoadGen expands into the gap.

Step 5 is the strongest evidence available. A script burning a flat percentage
would visibly drop when the background load went away.

Then the other side. Set a Target below the current Baseline and confirm
LoadGen contributes nothing, prints one warning, and keeps running:

```
warning: ram target 40 is below the current Baseline (57); contributing nothing
```

One warning per change, not one per tick.

## Step 5 — live retargeting

`targets.json` is written from the CLI at launch and is the sole source of
truth afterwards, re-read every 2 s.

With a run in progress, edit a Dial in the file and confirm the new Target
takes effect within two ticks without a restart. Confirm both directions.
Downward matters most: blocks must be released, not merely stop growing. Watch
`memory.used` and Task Manager, not just the console.

Then save deliberately invalid JSON and confirm the last good values are held
and one warning is printed.

## Step 6 — release on exit

After a run holding all four Dials, stop with Ctrl+C and confirm within 5
seconds:

- the final `released:` line appears
- Task Manager cpu and ram return to the step 1 Baseline
- `nvidia-smi` shows utilisation and `memory.used` back at Baseline
- no orphan `python.exe` remains in Task Manager Details

Repeat once with an interrupt during a large RAM fill, the case most likely to
strand memory.

## Step 7 — Users derivation

```
python loadgen.py --users 15 --duration 2m --log analysis\users15.csv
```

Confirm the derived Targets match the documented arithmetic against real host
capacity:

```
gpu  = users x per_user.gpu
cpu  = users x per_user.cpu
vram = users x per_user.vram_mb / total_vram_mb x 100
ram  = users x per_user.ram_gb  / total_ram_gb  x 100
```

For 15 users on this host that is gpu 60, cpu 90, vram about 55, ram about 34.

The shipped User Profile — gpu 4, cpu 6, vram_mb 300, ram_gb 2.5 — has never
been measured. Confirming the arithmetic is correct is not confirming the
answer is right. No density figure derived from `--users` means anything until
one real user has been measured on this host and those four numbers replaced.

## Evidence

Screen captures and CSVs go in `analysis\evidence\`, named
`run-<date>-<time>-<flags>` so a reading traces back to the run that produced
it. The existing capture is
`analysis\evidence\run-2026-09-09-1447-gpu100-vram100.png`.

## Findings document

Per Dial: does the Actual agree with an independent Consumer, does it converge,
how accurately does it hold, does it release.

Plus a verdict on the Baseline absorption test, the observed NVML to Task
Manager divergence, and any defect found in LoadGen itself.

Answer step 0 explicitly and near the top: is the gpu Actual trustworthy on
this host, and if not, what does the gpu Dial honestly claim. If no utilisation
figure is available to the guest, `README.md` and `GUIDE.md` need changing to
match, and those changes are committed.

## What not to conclude

NVML utilization on an A10-8Q reports this vGPU's share of its own scheduling
time, not a share of the physical A10. It says nothing about the two
neighbouring VMs and it is not a per-user measure.

A single capture of one run shows the tool executes. It does not show that it
holds, converges, absorbs Baseline changes, or releases.
