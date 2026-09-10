# What is left to do, in plain language

The job is to prove that LoadGen's four Dials do what they say: you ask for a
percentage, and the tool a density tester actually looks at shows that
percentage. Four Dials, four resources — GPU, VRAM, CPU, RAM.

This page says what has been proved, what has not, and what to run next. The
commands live in `docs\verification-scratchpad.md`. The reading rules live in
`docs\verification-runbook.md`.

---

## Where things stand

**Settled.** The GPU meter question. LoadGen used to read the GPU through
NVIDIA's own interface, NVML. On this A10-8Q that reading is junk: under a
constant flat-out load it bounced between 0 and 100 and averaged a different
number on every run. LoadGen now reads the same counter Task Manager shows,
which followed the load properly. NVML is still printed, in brackets, so you
can see it disagreeing.

**Settled.** The memory leak into system RAM. Filling the graphics card's
memory used to quietly spill into the machine's ordinary RAM — 88 GB of it,
once. LoadGen now checks each new block and stops when the card is genuinely
full.

**Settled.** The box gets its memory back. After the spill, shared GPU memory
is back to 0.1 GB, so nothing was stranded.

**Not settled.** Everything about whether the Dials actually converge and hold.
The GPU run on 9 September looked perfect, and that turned out to be the
problem — see below.

---

## The trap in the run that looked perfect

The 3-minute GPU run at Target 40 reported no error worth mentioning. It was
not evidence.

LoadGen starts the GPU Dial at a sensible guess: if you ask for 40, it starts
working 40% of the time. On this machine that guess lands almost exactly on
40% as measured. So the Dial arrived at the right answer before the correcting
loop did anything at all. The report said "converged in 0 seconds" because the
very first reading was already correct.

A cold start can never test the loop. **The Target has to move while the tool
is running.** That is the next run.

The same run also picked a bad number. This host is not idle — it is an RDP
session, and the desktop alone keeps the GPU at 12 to 17%, spiking to 39%.
Asking for 40 puts the load inside the background noise. Asking for 80 does
not.

---

## The next four things to run, in order

### 1. Move the Target while it runs

Start the GPU Dial at 80. Two minutes in, change it to 40. Two minutes later,
change it back to 80. Watch the number climb and fall.

This is the only test that shows the correcting loop working. It also covers
the claim that you can retune a running test without restarting it.

**Done looks like:** the new Target takes effect within about 4 seconds, the
reading reaches it within 20 seconds each way, and the internal `gpu_duty`
figure in the log visibly moves instead of sitting still.

**Expect 80 to read about 72.** The counter runs at roughly 0.9 times the
work actually done. That is a known offset, not a failure.

### 2. Check the VRAM reading against Task Manager

LoadGen printed VRAM at 29% while Task Manager showed about 1.5 of 7.5 GB,
which is 20%. Both are probably right — LoadGen counts memory Task Manager
leaves out of that particular figure — but "probably" is not good enough for a
findings document.

Read both at the same moment and settle it.

**Done looks like:** LoadGen's percentage matches `nvidia-smi`'s used-over-
total within a point. If it does, Task Manager's Dedicated figure is the odd
one out and the findings say so.

### 3. Prove the Baseline idea works

This is the central design claim. When you ask for 70%, LoadGen is not
supposed to add 70% on top of whatever is already running — it is supposed to
top the machine up *to* 70% and keep it there as the background moves.

Run a Dial at a Target, add some unrelated load by hand, and check that
LoadGen backs off rather than piling on. Then take the load away and check it
fills the gap again.

**Done looks like:** the monitoring tool stays on the Target throughout,
rather than climbing when you add load.

### 4. Prove it lets go

Stop a run that is holding all four Dials, including one part-way through a
big RAM fill. Check the machine returns to where it started and no stray
`python.exe` is left behind.

**Done looks like:** back to the idle figures within 5 seconds, and nothing
left in Task Manager's Details tab.

---

## After those, the paperwork

**Finish `docs\verification-findings.md`.** One section per Dial answering
four questions: does the number LoadGen prints match what a monitoring tool
shows, does it get to the Target, does it stay there, and does it release.

**Fix README.md and GUIDE.md.** Both still tell the reader that LoadGen steers
by NVML and that the Task Manager figure is shown for interest only. That is
backwards now. The findings list the exact sections.

**Rewrite step 0 of the runbook.** It currently asks whether the GPU reading
can be trusted. That question has an answer, so it should state the answer.
It should also stop telling people to look for a `Compute_0` graph in Task
Manager — this host does not have one. The work shows up under `3D`.

---

## The one thing to keep in mind

Every reading on this host is taken through an RDP session, and the session
itself uses the machine. The CPU sat between 11 and 29% doing nothing, and the
GPU between 12 and 17%. That is the Baseline, it is noisy, and it is why
Targets for these tests should be well clear of it.
