# Conversation log: AllTalk mode switch in the mic panel, and the panel's close hang

**Date:** 2026-09-30
**Started from:** `REPO_koboldccp_sst_tts_media` (Claude Code session)
**Code changed in:** `mic_panel.py`, `REPO_claude_code_voice_mode_Fix_LOG.md`
**Commits:** `13e2c25` (mic panel), `75462ee` (fix log)
**Companion logs:** `REPO_alltalk/logs/conversation_2026-09-30_alltalk_cpu_cuda_mode.md` (the
main record: why, the .bat, all test results), `REPO_koboldccp_sst_tts_media/logs/conversation_2026-09-30_alltalk_cpu_cuda_mode.md`

---

## 1. Why

Voice mode's AllTalk plus ComfyUI image generation was too much for the 12 GB graphics card.
AllTalk can now run on the main processor, switched from `REPO_alltalk\launch_alltalk.bat`.
The user asked that every app running that .bat show the mode and offer the switch.

## 2. What changed in `mic_panel.py`

- **Mode line and button** under the Restart buttons: `AllTalk: on the main processor (CPU)`
  (or graphics card, or `not running (saved: ...)`), refreshed every 3 s, and a
  `Switch AllTalk to ...` button. The button writes `REPO_alltalk/settings/device_mode.txt`;
  the .bat restarts AllTalk and the panel waits until it is back.
- **The .bat's own keys** (`C` / `P` / `R` / `Q` / `M`) work inside the panel's AllTalk pane,
  since the pane is a real console. The 5-second startup countdown also shows there.
- **Restart AllTalk / Close AllTalk / Close All:** when the .bat is running AllTalk, the panel
  writes `restart` / `stop` to `REPO_alltalk/runtime/request.txt` and the .bat does it. If the
  .bat is not running it, the old kill-and-relaunch / Ctrl+C paths are used.
- Also in the commit: this morning's uncommitted edit that starts AllTalk through
  `REPO_alltalk\launch_alltalk.bat` (user decision: commit the whole file in this repo).

## 3. Bug fixed: the panel never exited after Close All

**Symptom:** after `Close All` (or Quit), the window disappeared but the `pythonw` process stayed
forever (seen three times with today's changes). The Whisper and AllTalk panes were also blank.
The panel's code as it was before today's changes was tested once: its panes were blank too,
but it did exit normally that time. So it is not established whether the hang could happen
before today's changes; the cause below is a timing race, so one clean exit does not rule it
out.

**Cause (from a `faulthandler` stack dump of the stuck process):** the audio thread logs
"Shared audio stream closed" as it shuts down. The on-screen log handler passed that line to
the window with a Tk call from the audio thread, which waits for the window's thread to run
it - but the window was closing, so it never did. Python's exit then waited on that handler's
lock forever.

**Fix:** `TextHandler` now only puts lines on a queue; the window's own thread takes them off
every 100 ms. No other thread calls Tk. (A first attempt - removing the handler just before
closing the window - did not work and was reverted; see the fix log.)

## 4. Tests

| Test | Result |
|---|---|
| Panel auto-starts AllTalk | countdown shown in the AllTalk pane, saved mode used |
| Switch button (CUDA -> CPU) | back on the main processor in 14 s; no AllTalk process on the GPU |
| C pressed inside the AllTalk pane | switched to the graphics card, ready in 21 s |
| Freya via `/v1/audio/speech` (voice mode's request) | graphics card 2.8 s, main processor 14 s, same line |
| Restart AllTalk button | restarted through the .bat in 22 s, same mode |
| Close AllTalk Only | stopped at once, port free, runtime files removed |
| Close All (fixed build, twice) | panel exited in 6 s both times; nothing left running |
| Whisper and AllTalk panes | now show their text |

## 5. Not committed

`claude_code_voice_mode_mcp_server.py` has separate uncommitted edits that were not part of
this work.
