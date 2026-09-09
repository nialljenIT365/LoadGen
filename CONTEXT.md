# NV SKU GPU Testing

A synthetic load generator for an Azure Virtual Desktop session host (NV12ads_A10_v5) that holds GPU, CPU and RAM utilization at chosen percentages so autoscale rules and monitoring can be tested without real users.

## Language

**Dial**:
One of the four independently adjustable resource controls: GPU, VRAM, CPU, RAM. A dial set to 0 leaves that resource untouched.
_Avoid_: knob, slider, setting

**GPU**:
The dial for GPU compute utilization: the share of time the vGPU is executing a kernel. Excludes memory.
_Avoid_: GPU load, GPU usage

**VRAM**:
The dial for frame buffer occupancy: the share of the vGPU's dedicated memory that is allocated.
_Avoid_: GPU memory, frame buffer %, dedicated memory

**Target**:
The percentage a Consumer should display for a resource. It is the overall figure, not the script's own footprint.
_Avoid_: goal, level, setpoint

**Baseline**:
The share of a resource used by everything other than the script. The script fills the gap between Baseline and Target.
_Avoid_: idle load, background, overhead

**Self-check**:
The loop that reads each Actual, prints it beside the Target, and corrects the script's contribution to close the gap.
_Avoid_: feedback loop, monitor, controller

**Actual**:
The percentage a Consumer currently reports for that resource, read back by the self-check.
_Avoid_: measured, observed, reading

**Consumer**:
A monitoring tool whose displayed figure must track the target. In scope: Task Manager, Windows performance counters (Perfmon, Resource Monitor), nvidia-smi.
_Avoid_: monitor, watcher, reader
