# Profiler measures its own session from inside, as a standard user

The Profiler needs the cost of one Reference Session to produce a User Profile. Rather than subtracting a pre-logon Baseline from whole-host figures (the method loadgen's self-check uses), the Profiler runs inside the Reference Session, as that user, and sums the processes that share its Windows session ID. We chose this because it needs no admin rights, no separate Baseline capture step, and is unaffected by anything else running on the host, at the cost of missing SYSTEM-owned per-session processes (dwm, csrss) whose memory a standard user cannot read.

## Consequences

- Per-session RAM is slightly under-reported; the Profiler counts skipped processes and records the number in the output meta.
- GPU utilization is attributed per process via NVML where the vGPU driver allows it, with GPU Engine performance counters as fallback; the method and a whole-host NVML-to-GPU-Engine ratio are recorded so the operator can see any scale mismatch.
- The Profiler cannot tell whether the host was otherwise idle; the GUIDE tells the operator to make it so.
