# Target is the Consumer's overall figure, not the script's footprint

The load generator exists to make Task Manager, Perfmon and nvidia-smi display chosen percentages so user density on the GPU SKU can be tested. A dial target therefore means the overall figure a Consumer shows, and the script contributes only the gap between the Baseline (OS, services, driver, compositor) and that figure, re-measuring every self-check tick. We rejected the deterministic alternative, where the script allocates or burns a flat percentage of the machine, because it would land the displayed figure above the target by an amount that drifts with background activity, which is exactly the number the tests care about.

## Consequences

- A target below the current Baseline cannot be met; the script clamps to zero contribution and warns.
- The script's own overhead (Python workers, CUDA context, self-check) is part of the Baseline and is absorbed automatically.
