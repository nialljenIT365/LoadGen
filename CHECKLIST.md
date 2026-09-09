# Reference Session checklist

name: office-generic
version: 1

This is the work one person does while `profiler.py` is running. The Profiler
measures what that work costs, and those numbers become the **User Profile**
that every density figure is derived from.

**Replace this list with your own app set before you trust the result.** A
generic office day is a starting point, not your workload. If your users live in
AutoCAD, Revit or a browser-based 3D tool, this checklist measures the wrong
thing and the User Profile will be too small.

## Before you start

- You are logged into the session host as a normal user, not an administrator.
- Nobody else is logged in and `loadgen.py` is not running.
- Open the apps you are about to use *after* starting the Profiler, not before —
  the cost of launching them is part of what a real user costs.

## The run — about 20 minutes

The first 60 seconds are discarded as warm-up, so do not worry about what you
are doing at the very start.

| # | Minutes | Do this |
|---|---|---|
| 1 | 0–2 | Sign in to the apps you would normally sign into: mail, Teams, the line-of-business web app. Leave them open. |
| 2 | 2–5 | Open a browser and load five tabs you would really have open. Scroll each one. Leave all five open for the rest of the run. |
| 3 | 5–10 | Open a document or spreadsheet and edit it for five minutes — type, scroll, reformat. Not an idle window. |
| 4 | 10–12 | Play a video for two minutes, full screen, sound on. Teams recording, a YouTube clip, a training video — whatever your users watch. |
| 5 | 12–17 | Join a video call with camera on, or the nearest equivalent you can do alone: start a Teams meeting with yourself and share your screen. Skip only if there is genuinely no equivalent. |
| 6 | 17–19 | Leave everything open and do nothing for two minutes. An idle session with fifteen windows open is not free, and that cost is real. |
| 7 | 19–20 | Stop the Profiler with Ctrl+C. |

Do not close the apps before stopping the Profiler. Step 6 is measuring the
resting cost of a fully loaded session, which is what most of a working day
actually looks like.

## After the run

The Profiler writes two files into `%USERPROFILE%\Documents\LoadGen\`. Give the
`.json` to whoever is running the density test — see Step 8 of `GUIDE.md`.

## Changing this checklist

Edit the steps, then bump the `version:` line at the top of this file. The
Profiler records `name` and `version` in the output, so a profile can always be
traced back to the workload it came from. Two profiles taken with different
checklist versions are not comparable.
