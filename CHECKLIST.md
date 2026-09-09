# Reference Session checklist

name: design-engineering
version: 1

This is the work one person does while `profiler.py` is running. The Profiler
measures what that work costs, and those numbers become the **User Profile**
that every density figure is derived from.

[docs/reference-session-checklist.html](docs/reference-session-checklist.html)
is the same list as a tick sheet you can open in a browser or print. The two are
kept in step; if you change one, change the other.

## Before you start

- **Pick one persona for this run — Standard or GPU.** Run them as separate
  Reference Sessions. Mixing both in one session measures a user who does not
  exist.
- Sign in to the session host as the account that will do the work, and close
  anything left over from a previous session. Nobody else is logged in, and
  `loadgen.py` is not running.
- Start the Profiler from that same session:

  ```
  python profiler.py --duration 20m
  ```

  It sums only the processes sharing its own Windows session ID, so it has to be
  started from inside the Reference Session, not from an admin window.
- Let the first **60 seconds** pass before opening anything — that warm-up is
  discarded, so sign-in and shell startup are not counted as user cost.
- Open the apps *after* starting the Profiler, not before. Launching them is
  part of what a real user costs.

Twenty minutes will not cover forty applications end to end. Open what this
persona genuinely uses, work in each one properly, and leave it open — memory
that stays allocated is what the **p95** figures are for.

## Standard apps

For a Reference Session on any pool.

| # | Do this |
|---|---|
| 1 | Open **7-Zip** and extract an archive of a few hundred MB. |
| 2 | Open **Adobe Acrobat (SDL) 26.1**, load a long PDF, scroll and search it. |
| 3 | Open **Adobe Bridge 16.0.2 SDL** and browse a folder of images until thumbnails finish building. |
| 4 | Open **Adobe Illustrator 30.2.1 SDL**, open a real artboard and pan and zoom it. |
| 5 | Open **Adobe InCopy 21.2 SDL** and edit a story. |
| 6 | Open **Adobe InDesign 21.2 SDL**, open a multi-page document and page through it. |
| 7 | Open **Adobe Photoshop 27.4 SDL**, open a layered file and apply a filter. |
| 8 | Open a protected document with the **FileOpen Client**. |
| 9 | Open **Firefox** with a handful of tabs, including one video. |
| 10 | Open **Google Chrome** with a realistic tab count — this is usually the largest single memory cost. |
| 11 | Open **Mendeley Reference Manager** and let the library sync. |
| 12 | Open **Microsoft Teams** and join a call with camera and screen share on. A Teams call is the single biggest driver of a standard user's CPU figure — do not skip it. |
| 13 | Open **VLC Media Player** and play a video for a few minutes. |

**Running already, nothing to open:** Sophos AV and the WXP Insights Agent both
start with the host. They are part of the **Baseline**, not user cost.

## GPU apps

For a Reference Session on the GPU pool.

| # | Do this |
|---|---|
| 1 | Open **Adobe After Effects 26.0 SDL**, load a composition and scrub the timeline. |
| 2 | Open **Adobe Animate 24.0.12 SDL** and play back a timeline. |
| 3 | Open **Adobe Audition 26.0 SDL** and play a multitrack session. |
| 4 | Open **Adobe Character Animator 26.0 SDL** and run a live puppet preview. |
| 5 | Open **Adobe Dimension 4.1.8 SDL** and render a scene preview. |
| 6 | Open **Adobe Lightroom Classic 15.2 SDL** and move through the Develop module on several photos. |
| 7 | Open **Adobe Media Encoder 26.0.2 SDL** and run an export to completion. An encode is sustained GPU load rather than a spike — good for the mean the User Profile uses for `gpu`. |
| 8 | Open **Adobe Premiere Pro SDL 25.6.3**, scrub a sequence and play it back. |
| 9 | Open **Adobe Substance 3D Modeler 1.22.6 SDL** and sculpt in the viewport. |
| 10 | Open **Adobe Substance 3D Painter 12.0.0 SDL** and paint on a textured model. |
| 11 | Open **Adobe XD 61.0.12 SDL** and move through a prototype. |
| 12 | Open **Autodesk 3ds Max**, load a scene and orbit the viewport. |
| 13 | Open **Autodesk AutoCAD** and pan and zoom a real drawing. |
| 14 | Open **Autodesk Civil 3D** and navigate a surface model. |
| 15 | Open **Autodesk Fusion** and rotate an assembly. |
| 16 | Open **Autodesk Navisworks** and walk a federated model. |
| 17 | Open **Autodesk ReCap Pro** and load a point cloud. Point clouds are the heaviest VRAM case in the Autodesk set — worth including if anyone on this pool uses it. |
| 18 | Open **Autodesk Revit**, load a model and orbit a 3D view. |
| 19 | Open **Autodesk Robot Structural Analysis** and run an analysis. |
| 20 | Open **Keyshot Studio 2026.2** and run a render. |
| 21 | Open **Rhino 8** and orbit a model in a shaded viewport. |
| 22 | Open **SketchUp Pro 2026** and navigate a model. |
| 23 | Open **SOLIDWORKS** and rotate an assembly. |
| 24 | Open **Twinmotion** and move through a scene in real time. Real-time rendering holds the GPU near its ceiling — expect this one to set the peak. |
| 25 | Open **ArcGIS Pro 3.7.1901**, load a map and pan and zoom it. |

**No entry needed:** Google Fonts is a font package, not an application to
exercise.

## Finish the run

- Let `--duration` end the run, or press **Ctrl+C**. Either way the files are
  written.
- Do not close the apps first. A fully loaded session sitting idle is not free,
  and that resting cost is what most of a working day actually looks like.
- Confirm the `.json` and `.csv` exist at the paths the Profiler printed in its
  `wrote` lines.
- Read the summary before trusting it: `too_short` must be **false**, and
  `skipped_processes` should be a small number. If `gpu_available` is false on a
  GPU run, the GPU and VRAM figures are not measurements — re-run once the GPU
  check in `GUIDE.md` Step 1 passes.

## After the run

The Profiler writes two files into `%USERPROFILE%\Documents\LoadGen\`. Give the
`.json` to whoever is running the density test — see Step 8 of `GUIDE.md`.

## Changing this checklist

The app list above is one image's. If yours differs, edit the steps to match it
— a User Profile is only as good as the workload it was measured against.

After editing, bump the `version:` line at the top of this file, and make the
same change in
[docs/reference-session-checklist.html](docs/reference-session-checklist.html).
The Profiler records `name` and `version` in the output, so a profile can always
be traced back to the workload it came from. Two profiles taken with different
checklist versions are not comparable.
