# LoadGen — setup and run guide

A step-by-step guide for an IT admin setting up `loadgen.py` on an Azure
Virtual Desktop (AVD) session host for the first time.

**What this tool does.** It puts fake load on a session host so you can see how
many users the machine would hold, without needing real users. You tell it "make
the GPU read 60%" and it holds the machine there until you stop it.

**What you need before you start:**

- An RDP connection to an already-provisioned AVD session host. Building the VM
  or the host pool is out of scope for this guide — someone has already done it.
- Local administrator rights on that host.
- The host should be an **NV12ads_A10_v5**: 12 vCPU, 110 GB RAM, and one
  **A10-8Q vGPU** with an 8 GB frame buffer. A vGPU is a slice of a physical
  NVIDIA card shared between several VMs; the "8Q" part means this VM gets 8 GB
  of that card's memory.
- A copy of this repository on the host (clone it, or copy the folder over).

**Time:** about 20 minutes, most of it waiting for downloads.

Work through the sections in order. Each one ends with a **Check** — do not move
on until the check passes.

---

## Vocabulary you will see in this guide

These terms are used precisely and always mean the same thing. The full list is
in [CONTEXT.md](CONTEXT.md).

| Term | Meaning |
|---|---|
| **Dial** | One of the four things you can turn up: `gpu`, `vram`, `cpu`, `ram`. A Dial set to 0 leaves that resource alone. |
| **Target** | The percentage you want a monitoring tool to *display*. |
| **Actual** | The percentage that tool currently *does* display, read back by the tool. |
| **Baseline** | Everything the machine was already using before LoadGen started. |
| **Self-check** | The loop, once every 2 seconds, that reads each Actual, prints it next to the Target, and adjusts. |
| **Consumer** | A monitoring tool whose number has to match: Task Manager, Perfmon, `nvidia-smi`. |
| **Users** | A count of simulated users. Sets the Targets when you do not set them yourself. |
| **User Profile** | What one typical user costs, as one number per resource. Multiplied by Users to get Targets. |

**The one idea that matters.** A Target is the **overall** figure a Consumer
shows — not LoadGen's own share of it. Ask for `cpu 60` on a machine already
sitting at 15%, and LoadGen adds roughly 45 so Task Manager reads 60. It
re-measures every 2 seconds and keeps correcting. The reasoning is in
[docs/adr/0001-target-is-consumer-figure.md](docs/adr/0001-target-is-consumer-figure.md).

One consequence to know now: **if you ask for a Target lower than the Baseline,
LoadGen cannot get there.** It will not shut anything down to make room. It says
so and carries on. That is covered in Step 5.

---

## Step 1 — Verify the GPU is actually working

**Do this before installing anything.** If the vGPU is not present, not
licensed, or not visible to the guest OS, everything later in this guide will
fail in a confusing way. Five minutes here saves an hour later.

RDP into the session host and open **PowerShell**.

### 1a. Run nvidia-smi

`nvidia-smi` (NVIDIA System Management Interface) ships with the NVIDIA driver
and reports the GPU's state.

```powershell
nvidia-smi
```

### 1b. Read the output

A healthy result has two parts. First a **header**, which looks like this
(exact numbers vary by driver version):

```
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 550.90.07              Driver Version: 552.74         CUDA Version: 12.4      |
|-----------------------------------------+------------------------+----------------------+
```

Then a **device table** listing at least one GPU:

```
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|=========================================+========================+======================|
|   0  NVIDIA A10-8Q                  On  |   00000002:00:00.0 Off |                    0 |
| N/A   N/A    P0             N/A / N/A   |     560MiB /  8192MiB  |      0%      Default |
+-----------------------------------------+------------------------+----------------------+
```

Three things to confirm:

1. **A device is listed.** The Name should mention **A10-8Q** — the vGPU
   profile this VM was given. A table with no rows under it means the driver
   loaded but cannot see a GPU.
2. **Memory-Usage shows roughly `8192MiB` total.** That is the 8 GB frame
   buffer. A much smaller number means a different vGPU profile than expected —
   stop and check with whoever built the host.
3. **Write down the `CUDA Version` from the header.** In the example above it is
   `12.4`. **You need this number in Step 4.** It is not the driver version and
   it is not the NVIDIA-SMI version — it is the third field on that top line.

> Fields showing `N/A` for fan, temperature and power are **normal** on a vGPU.
> The guest sees a virtual slice, not the physical card's sensors.

### 1c. Confirm the GRID guest driver is present and licensed

The **GRID guest driver** is the NVIDIA driver built for virtualised GPUs. It
needs a licence checkout from an NVIDIA licence server to run at full speed —
unlicensed, the GPU throttles hard and your test numbers will be meaningless.

```powershell
nvidia-smi -q | Select-String -Pattern "License|Driver Model|vGPU"
```

On a licensed host you should see a licence status reported as **Licensed**. If
it reports unlicensed, expired, or the section is missing entirely, the guest
has not checked out a licence. Raise that with whoever manages the NVIDIA
licence server before continuing — you can complete this guide on an unlicensed
host, but the density numbers it produces will be wrong.

You can also confirm the driver is loaded from Windows' own view:

```powershell
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, Status
```

The A10 should appear with `Status: OK`.

### Check — do not continue until all of these are true

- [ ] `nvidia-smi` runs and prints a header.
- [ ] At least one device row is listed, naming an **A10-8Q**.
- [ ] Total memory reads about **8192MiB**.
- [ ] You have written down the **CUDA Version** from the header.
- [ ] The GRID driver reports a licence.

### If the check fails

| Symptom | What it means | What to do |
|---|---|---|
| `nvidia-smi : The term 'nvidia-smi' is not recognized...` | The NVIDIA driver is not installed, or its folder is not on PATH. | Try the full path: `& "C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"`, and also `& "$env:SystemRoot\System32\nvidia-smi.exe"`. If neither exists, the driver is genuinely absent — install the NVIDIA GRID guest driver matched to the host's vGPU version, reboot, and retry. |
| `Failed to initialize NVML: Unknown Error` | The driver is installed but cannot talk to the device. | Reboot the host and retry. If it persists, the driver version may not match the host's vGPU manager — that is a mismatch only the platform owner can fix. |
| `No devices were found` | The driver loaded but no vGPU is attached to this VM. | The VM has not been given a vGPU, or it is disabled. Check Device Manager for a disabled or errored display adapter. Otherwise this is a VM configuration problem — escalate. |
| Device Manager shows the A10 with a yellow warning triangle | Driver failed to start (often error code 43). | Reinstall the GRID guest driver, reboot, retry. |
| Licence status is unlicensed or expired | The guest cannot reach the NVIDIA licence server, or the token is missing. | Escalate to whoever manages NVIDIA licensing. Do not proceed if you need trustworthy numbers. |

---

## Step 2 — Install Python

Python is **not** installed on a fresh session host. LoadGen needs Python 3.12
specifically — not the newest release — because that is the version the GPU
libraries ship wheels for.

```powershell
winget install Python.Python.3.12
```

**Then close PowerShell and open a new one.** The installer adds Python to PATH,
and an already-open shell will not see it.

### Check

```powershell
py -3.12 --version
```

Expected:

```
Python 3.12.x
```

If you get `py : The term 'py' is not recognized`, you are still in the old
shell — open a new one. If a new shell still fails, re-run the `winget` command
and read its output for an error.

---

## Step 3 — Create the virtual environment

A virtual environment ("venv") is a private folder of Python packages for this
one tool, so installing it cannot disturb anything else on the host.

From inside the repository folder:

```powershell
cd C:\path\to\LoadGen
py -3.12 -m venv .venv
.venv\Scripts\activate
```

### Check

Your prompt should now be prefixed with `(.venv)`:

```
(.venv) PS C:\path\to\LoadGen>
```

Confirm the venv's Python is the one in use:

```powershell
python --version
```

Expected: `Python 3.12.x`.

If activation is refused with a message about execution policy, allow signed
local scripts for your account and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

> **Every later command in this guide assumes `(.venv)` is on your prompt.** If
> you close the shell, `cd` back to the repo and run `.venv\Scripts\activate`
> again.

---

## Step 4 — Install the packages

Two installs, in this order.

### 4a. torch — pick the index URL from your CUDA Version

**torch** (PyTorch) is the library that runs work on the GPU. It ships in
versions built against a specific CUDA release, so you have to fetch it from the
matching index. Use the **CUDA Version** you wrote down in Step 1b:

| CUDA Version in the `nvidia-smi` header | Command to run |
|---|---|
| **12.6 or newer** | `pip install torch --index-url https://download.pytorch.org/whl/cu126` |
| **older than 12.6** | `pip install torch --index-url https://download.pytorch.org/whl/cu124` |

This is a large download — expect a few minutes.

> **Why isn't this pinned in `requirements.txt` like everything else?** Because
> the right answer depends on the driver installed on *this* host, and
> `requirements.txt` cannot know that. It is a deliberate gap, not an omission.

### 4b. The rest

```powershell
pip install -r requirements.txt
```

That installs `psutil==7.2.2` (reads CPU and RAM) and
`nvidia-ml-py==12.575.51` (reads the GPU through NVML, the same interface
`nvidia-smi` uses).

### Check — this is the important one

```powershell
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
```

Expected — the first line is your torch version, and **the second line must be
`True`**:

```
2.x.x+cu126
True
NVIDIA A10-8Q
```

If the second line is `False`, torch installed but cannot reach the GPU. Do not
continue — the GPU and VRAM Dials will not work. Common causes:

- You installed the **CPU-only** build. A version string with no `+cu` suffix
  (e.g. `2.6.0` rather than `2.6.0+cu126`) means the `--index-url` was missing
  or mistyped. Run `pip uninstall torch`, then redo Step 4a.
- You picked the wrong index for the driver. Re-read the CUDA Version from
  Step 1b and try the other row of the table.
- The GPU check in Step 1 did not actually pass. Go back and repeat it.

Also confirm NVML is reachable:

```powershell
python -c "import pynvml; pynvml.nvmlInit(); print(pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(0)))"
```

This should print the device name. An error here means `nvidia-ml-py` cannot
open the GPU — the same causes as above apply.

---

## Step 5 — First run

Start with a short CPU-only run. It touches no GPU, so it isolates "is LoadGen
working" from "is the GPU working".

```powershell
python loadgen.py --cpu 30 --duration 60s
```

### What you will see

Real output from a 12-vCPU machine. This capture used `--duration 12s` to keep
it short; a 60-second run looks identical with more ticks:

```
targets file: C:\path\to\LoadGen\targets.json (re-read every 2s)
cpu: 12 workers started
10:57:51  gpu   0/ -- (tm  --) | vram   0/ -- | cpu  30/100 | ram   0/ 60
10:57:53  gpu   0/ -- (tm   3) | vram   0/ -- | cpu  30/ 36 | ram   0/ 60
10:57:55  gpu   0/ -- (tm   3) | vram   0/ -- | cpu  30/ 32 | ram   0/ 60
10:57:57  gpu   0/ -- (tm   3) | vram   0/ -- | cpu  30/ 30 | ram   0/ 60
10:57:59  gpu   0/ -- (tm   4) | vram   0/ -- | cpu  30/ 41 | ram   0/ 61
10:58:01  gpu   0/ -- (tm   5) | vram   0/ -- | cpu  30/ 34 | ram   0/ 61
10:58:03  gpu   0/ -- (tm   5) | vram   0/ -- | cpu  30/ 30 | ram   0/ 61
duration reached after 12s
released: cpu workers stopped, ram and vram blocks dropped
```

### How to read a line

```
10:57:57  gpu   0/ -- (tm   3) | vram   0/ -- | cpu  30/ 30 | ram   0/ 60
   |         |    |   |    |          |            |    |
   |         |    |   |    |          |            |    +-- ram Actual: 60% in use
   |         |    |   |    |          |            +------- ram Target: 0, left alone
   |         |    |   |    |          +-------------------- cpu Target 30 / Actual 30
   |         |    |   |    +------------------------------- Task Manager's GPU figure
   |         |    |   +------------------------------------ gpu Actual unreadable here
   |         |    +---------------------------------------- gpu Target: 0, left alone
   |         +--------------------------------------------- the Dial name
   +------------------------------------------------------- wall-clock time
```

Every Dial prints as **`target/actual`**. One line every 2 seconds — that is one
Self-check tick.

- **`--`** means the value could not be read. On a CPU-only run with no GPU
  activity that is expected for `gpu` and `vram`.
- **`tm`** is Task Manager's GPU figure, taken from the Windows **GPU Engine**
  performance counter. It is shown for information only — LoadGen steers by the
  NVML number (the `gpu` Actual), not by `tm`. The two routinely differ by 10 to
  20 points because they measure through different layers. Both are printed so
  the gap is visible rather than surprising.
- **The first `cpu` reading is always wrong** — `100` in the sample above. CPU
  percentage is measured over the interval since the last reading, and that
  interval includes LoadGen starting its 12 worker processes. Ignore line one;
  judge from line two onwards.

### Check

- [ ] The Actual for `cpu` lands near the Target within three or four ticks
      (30/30, 30/32 — small wobble is normal).
- [ ] Task Manager's CPU figure agrees, roughly.
- [ ] After 60 seconds it prints `duration reached` and then `released:`.

### Expected: the below-Baseline warning

Ask for a Target lower than what the machine is already using and you will see
this once, before the first tick:

```
warning: ram target 20 is below the current Baseline (61); contributing nothing
```

**This is correct behaviour, not a fault.** The machine was already at 61% RAM;
LoadGen cannot ask Windows to give memory back, so it contributes nothing and
keeps running. The `ram` Dial simply stays at the Baseline. Raise the Target
above the Baseline — in `targets.json`, no restart needed — and it will start
contributing.

### Now test the GPU

```powershell
python loadgen.py --gpu 40 --vram 25 --duration 60s
```

> **Be aware:** the CPU, RAM, targets-file and clean-exit paths have been tested
> and work. **The GPU and VRAM paths have never been run against real NVIDIA
> hardware** — this host is their first run. Treat the results below as *what
> should happen*, and check them rather than assume them.

What should happen: `gpu: load thread started` appears at launch, the `gpu`
Actual climbs to sit near 40, and the `vram` Actual near 25.

How to tell it did not: open a second PowerShell window and watch the GPU
directly while the run is in progress.

```powershell
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv -l 2
```

That prints utilisation and used memory every 2 seconds. On the `--vram 25` run
above, `memory.used` should be roughly 25% of 8192 MiB — around 2000 MiB.

If `nvidia-smi` shows 0% utilisation and unchanged memory while LoadGen's own
`gpu` and `vram` Actuals also read 0 or `--`, the GPU path is not working on
this host. Capture the full console output and the `nvidia-smi` output and
report it — do not just move on, because every GPU density number after that
point would be fiction.

---

## Step 6 — The thing you will actually do: retarget a running test

**`targets.json` is the sole source of truth once a run has started.**

At launch LoadGen writes `targets.json` from whatever you typed on the command
line, overwriting any existing file. From that moment it re-reads the file
**every 2 seconds**. Editing the file retargets a running test — no restart, no
Ctrl+C, no losing the load you already built up.

This is the normal day-to-day operation: start a run, then walk the Dials up
step by step while you watch how the host behaves.

### Worked example

Start a run with no end time, driven by a user count:

```powershell
python loadgen.py --users 15
```

LoadGen writes this file:

```json
{
  "users": 15,
  "per_user": {
    "gpu": 4,
    "cpu": 6,
    "vram_mb": 300,
    "ram_gb": 2.5
  },
  "gpu": null,
  "vram": null,
  "cpu": null,
  "ram": null
}
```

The rules for that file:

- A **number** in a Dial is manual and wins.
- **`null`** in a Dial means "work it out from `users` × `per_user`".
- If `users` is absent or `null`, every Dial is manual and a `null` Dial reads
  as 0.

Leave the run going. Open `targets.json` in Notepad — **a second window, while
the run continues** — change `"users": 15` to `"users": 20`, and save.

Within two ticks (four seconds) the console shows the new Targets, and LoadGen
starts working towards them. Here is that happening on a live run — the `cpu`
Target was changed from 0 to 35 mid-run:

```
10:58:46  gpu   0/ -- (tm   9) | vram   0/ -- | cpu   0/ 12 | ram   0/ 60
10:58:48  gpu   0/ -- (tm  10) | vram   0/ -- | cpu   0/ 12 | ram   0/ 60
--- edited targets.json: cpu 0 -> 35 ---
10:58:50  gpu   0/ -- (tm   9) | vram   0/ -- | cpu  35/ 12 | ram   0/ 60
10:58:52  gpu   0/ -- (tm  10) | vram   0/ -- | cpu  35/ 41 | ram   0/ 60
10:58:54  gpu   0/ -- (tm   9) | vram   0/ -- | cpu  35/ 36 | ram   0/ 60
10:58:56  gpu   0/ -- (tm  11) | vram   0/ -- | cpu  35/ 36 | ram   0/ 60
10:59:00  gpu   0/ -- (tm   8) | vram   0/ -- | cpu  35/ 35 | ram   0/ 60
```

The Target changes on the very next tick; the Actual takes three or four ticks
to catch up.

You can also pin one Dial and derive the rest — set `"cpu": 90` and leave the
others `null`, and cpu holds at 90 while gpu, vram and ram follow the user
count.

### If you mistype the JSON

Save a half-finished edit and LoadGen warns once and **keeps the last good
values** — the load does not drop:

```
warning: cannot read C:\path\to\LoadGen\targets.json (<the JSON parser's complaint>); keeping last good values
```

The text in brackets is Python's own description of what is wrong with the file
— usually a missing comma or brace, with a line and column number.

Fix the file and save again. It confirms recovery:

```
C:\path\to\LoadGen\targets.json readable again
```

### Safety limits

`ram` and `vram` are capped at **95** — asking for more gives you 95. Passing
`--unsafe` removes the cap. Do not use it casually: filling RAM to 100% on a
session host can make it unresponsive to RDP, and you would need a hard reset
from the Azure portal to get back in. `gpu` and `cpu` may go to 100 without the
flag.

---

## Step 7 — Fix the User Profile before you trust any number

The **User Profile** is what LoadGen assumes one typical user costs. It is the
`per_user` block in `targets.json`, and the shipped values are:

```json
"per_user": { "gpu": 4, "cpu": 6, "vram_mb": 300, "ram_gb": 2.5 }
```

**These four numbers are a guess.** They were not measured on your workload. They
are there so `--users 15` produces *something* on the first run.

**Every density figure your testing produces is only as good as this profile.**
If the real per-user cost is half the guess, LoadGen will report the host holding
half as many users as it truly can — and someone will size a deployment on it.

### Measure the real numbers

1. Have one real person log into the session host and do genuinely
   representative work for 15–30 minutes — the actual apps, the actual
   documents, not an idle desktop.
2. While they work, watch what that one session costs:
   - **cpu** — Task Manager → Details, or Performance for the whole-machine
     figure. Record a typical sustained percentage, not the peak.
   - **ram_gb** — Task Manager → Users, expand the session, read its memory in
     GB.
   - **gpu** and **vram_mb** — run
     `nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv -l 5`
     with only that one user on the host, and subtract the idle Baseline you
     recorded before they logged in.
3. Replace all four numbers in the `per_user` block of `targets.json`.
4. Re-run `--users N`. Every Target now derives from measurement rather than
   assumption.

Repeat this whenever the app set on the host changes.

---

## Step 8 — Stopping, and confirming the load is released

### Stop it

Click the LoadGen console window and press **Ctrl+C**.

You will see:

```
interrupted
released: cpu workers stopped, ram and vram blocks dropped
```

If you set `--duration`, it stops on its own instead, printing
`duration reached after 60s` (with your elapsed seconds) before the same
`released:` line.

**The `released:` line is the confirmation.** It means all four Dials went to 0,
the CPU worker processes were terminated and joined, the RAM and VRAM blocks
were freed, and NVML was shut down. Exit completes within about 5 seconds.

### Confirm the machine is back to Baseline

Do not take the message on trust — verify:

1. **Task Manager → Performance.** CPU and Memory should fall back to what they
   read before you started. Give it 10–15 seconds; Windows reclaims memory
   lazily.
2. **Task Manager → Details.** There should be **no leftover `python.exe`
   processes**. LoadGen runs one worker per logical processor — 12 on this SKU —
   and they all go when it exits. If any survive, end them manually and note it,
   because that is a bug worth reporting.
3. **`nvidia-smi`.** `memory.used` should be back near its idle figure, and
   utilisation near 0.

Only once all three look normal is the host clean for the next test.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `nvidia-smi : The term 'nvidia-smi' is not recognized...` | Driver missing, or not on PATH. | Try the full path under `C:\Program Files\NVIDIA Corporation\NVSMI\`. If absent, install the GRID guest driver. See Step 1. |
| `Failed to initialize NVML: Unknown Error` | Driver loaded but cannot reach the device. | Reboot. If it persists, the driver may not match the host's vGPU manager — escalate. |
| `py : The term 'py' is not recognized` | PATH not refreshed after installing Python. | Close PowerShell, open a new one. |
| `.venv\Scripts\activate` refused, execution-policy message | PowerShell script execution is restricted. | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, then retry. |
| `torch.cuda.is_available()` returns `False` | CPU-only torch, or wrong CUDA index. | Check the version string has a `+cu` suffix. If not, `pip uninstall torch` and redo Step 4a with the right index URL. |
| `error: psutil is required. pip install -r requirements.txt` | The venv is not active, or Step 4b was skipped. | Activate the venv, then `pip install -r requirements.txt`. |
| `error: a gpu or vram target was requested but nvidia-ml-py is not installed (No module named 'pynvml').` | Step 4b was skipped. | `pip install -r requirements.txt`. Or run CPU/RAM only with `--gpu 0 --vram 0`. |
| `error: a gpu or vram target was requested but NVML could not open the GPU (...)` | NVML cannot reach the vGPU. | Re-run Step 1. The message's second line suggests checking the GRID guest driver. |
| `error: a gpu or vram target was requested but could not allocate the GPU work tensors (...)` | torch reached the GPU but could not get memory. | Something else is holding the frame buffer — check `nvidia-smi` for other processes. Or lower the `vram` Target. |
| `warning: ram target 20 is below the current Baseline (61); contributing nothing` | The Target is under what the machine already uses. | Expected, not a fault. Raise the Target above the Baseline. See Step 5. |
| `warning: cannot read ...targets.json (...); keeping last good values` | Invalid JSON saved. | Fix the syntax and save again; the load never dropped. |
| `warning: gpu/vram target set mid-run but ...; holding at 0` | A GPU Dial was raised in `targets.json` on a run with no GPU stack. | Deliberate — a long CPU/RAM test will not die over a typo. Restart with the GPU stack installed if you need that Dial. |
| `gpu` Actual stays `--` or 0 during a GPU run | NVML cannot read utilisation, or the GPU load never started. | Cross-check with `nvidia-smi -l 2` in a second window. This path is unverified on real hardware — capture both outputs and report. |
| `tm` differs from the `gpu` Actual by 10–20 points | Two different measurement layers. | Expected. LoadGen steers by the NVML figure. |
| First `cpu` reading is 100 | The measurement window covers LoadGen starting its workers. | Expected. Read from the second tick onward. |
| `python.exe` processes remain after exit | Workers were not reaped. | End them in Task Manager → Details, and report it — the clean-exit path is supposed to prevent this. |

---

## Command reference

```
python loadgen.py [--gpu N] [--vram N] [--cpu N] [--ram N]
                  [--users N] [--duration 90s|30m|2h] [--log PATH]
                  [--nice] [--unsafe] [--targets PATH]
```

| Flag | What it does |
|---|---|
| `--gpu N` | gpu Target, 0–100 |
| `--vram N` | vram Target, 0–100 (capped at 95) |
| `--cpu N` | cpu Target, 0–100 |
| `--ram N` | ram Target, 0–100 (capped at 95) |
| `--users N` | Simulated users; derives every Dial not given explicitly |
| `--duration` | Run time, e.g. `90s`, `30m`, `2h`. Default: until Ctrl+C |
| `--log PATH` | Append one CSV row per tick. Off by default |
| `--nice` | Run CPU workers at below-normal priority |
| `--unsafe` | Allow ram and vram Targets above 95 |
| `--targets PATH` | Use a different targets file. Default: `targets.json` beside the script |

`--log run1.csv` writes one row per tick:
`timestamp, users, gpu_t, gpu_a, gpu_tm, vram_t, vram_a, cpu_t, cpu_a, ram_t, ram_a`.
Use it whenever a run is producing numbers someone will act on.

---

## What is proven and what is not

Keep this in mind when you report results.

**Verified** on a development machine with no NVIDIA GPU: CPU Dial convergence,
RAM fill and release, the below-Baseline warning, invalid-JSON handling, live
retargeting through `targets.json`, `--duration`, and Ctrl+C exiting cleanly
with no orphan processes.

**Not verified — you are the first to run these:** the `gpu` Dial moving
`nvidia-smi` and Task Manager, the `vram` Dial reaching a given `memory.used`,
and `--users N` populating all four Dials on a real GPU host.

If a GPU result looks wrong, it may well be. Cross-check against `nvidia-smi`
before you believe a number, and report what you find.

---

## Further reading

- [README.md](README.md) — full reference, the assumptions made during the
  build, and what is out of scope.
- [CONTEXT.md](CONTEXT.md) — the complete vocabulary.
- [docs/adr/0001-target-is-consumer-figure.md](docs/adr/0001-target-is-consumer-figure.md)
  — why a Target is the Consumer's overall figure.
