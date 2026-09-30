# Fix Log: REPO_claude_code_voice_mode

## Bug: AllTalk Gradio UI not loading on port 7852

### Solutions Already Tried (DO NOT REPEAT)

| # | Solution | Result |
|---|----------|--------|
| 1 | Changed `gradio_interface: false` to `true` in confignew.json | NOT FIXED — AllTalk still prints Gradio URL but 7852 not listening |
| 2 | Suggested restarting AllTalk (multiple times) | NOT FIXED — user confirms doesn't work |
| 3 | Ran CP1 subagent — said "code is fine, just restart" | WRONG — restart was already tried |
| 4 | Ran second CP1 subagent — same conclusion | WRONG — repeated failed solution |
| 5 | Ran CP1API subagent — rejected, never completed | N/A |
| 6 | Multiple netstat/curl checks | Redundant — confirmed 7852 not listening, already known |
| 7 | Suggested gradio DLL corruption from fresh install | UNVERIFIED — never actually checked |

### Known Facts (from chat history)
- AllTalk freshly reinstalled (option 4 delete env + option 1 fresh install)
- Fresh install had DLL errors: gdk-pixbuf failures, PyQt6 "DLL load failed while importing QtWidgets"
- AllTalk TTS works on port 7851 (confirmed by TTS generation in console output)
- Gradio port 7852: NOTHING listening (netstat confirmed)
- confignew.json: `gradio_interface: true`, `launch_gradio: true`
- script.py line 731: `gradio_enabled = config.gradio_interface` (module-level)
- script.py line 2575: `if gradio_enabled is True:` (gates Gradio UI block)
- script.py line 1230: splash URL gated by `launch_gradio` (different key — prints URL even if Gradio doesn't start)
- AllTalk console shows NO errors about Gradio failing to start

## Bug: Mic panel not auto-starting services

### Solutions Already Tried (DO NOT REPEAT)

| # | Solution | Result |
|---|----------|--------|
| 1 | Added PYTHONUNBUFFERED=1 env to _launch_service | Services still didn't start |
| 2 | Added try/except to _do_auto_start thread | Error handling added but issue persists |
| 3 | Added report_callback_exception redirect | Added but issue persists |
| 4 | Added TextHandler.emit try/except | Added but not verified |
| 5 | CP1 subagent said "test agent left faked stubs, code is clean now" | UNVERIFIED |
| 6 | Reduced _is_service_running timeout to 0.5s | Not verified |

## Bug: Mic panel process never exits after "Close All" / Quit; terminal panes blank (2026-09-30)

### Solutions Already Tried (DO NOT REPEAT)

| # | Solution | Result |
|---|----------|--------|
| 1 | Remove the on-screen log handler in `_quit` / `_restart` just before `root.destroy()` | NOT FIXED - the audio thread was already inside the handler's Tk call when it was removed |
| 2 | `TextHandler` puts lines on a `queue.Queue`; the window thread drains it every 100 ms (`_poll`), so no other thread ever calls Tk | FIXED - panel exits ~6 s after Close All (tested twice); Whisper/AllTalk panes now show their text |

### Known Facts
- `faulthandler` dump of the stuck process: audio `monitor` thread blocked in `tkinter.after()` inside `TextHandler.emit` (logging "Shared audio stream closed"); main thread blocked in `logging.shutdown` waiting for that handler's lock.
- A Tk call from a non-window thread waits for the window thread to run it; once the window is closing, it never does.
