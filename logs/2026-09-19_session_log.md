# Session log — 2026-09-19

Two pieces of work, in this order: fixing a broken console-reading helper and
writing down how console attachment actually works, then moving this app's
logging out of the shared folder and into this repo. A third thing surfaced on
its own at the end and is recorded because it matters more than either.

Everything below is what actually happened, including the two false starts and
the one place where the approved plan turned out to rest on a wrong belief.

---

## 1. Starting point

The session opened with a pasted error — four complaints from a PowerShell
helper called `conread.ps1`, living in a scratchpad under the comfyUI repo. The
script's job was to read the text on screen inside another program's console
window, which is how the launcher's `Choose [1-9]:` menu had been read without
taking a screenshot.

The first message quoted the errors in full. The lead one:

```
Cannot convert argument "dwDesiredAccess", with value: "-2147483648", for
"CreateFileW" to type "System.UInt32": "Cannot convert value "-2147483648" to
type "System.UInt32". Error: "Value was either too large or too small for a
UInt32.""
```

followed by three more about `hConsoleOutput` and `h` being null.

Worth recording as process, not just outcome: the first reply asked what was
wanted instead of diagnosing. That was the wrong instinct and was called out —
when an error is pasted, the expectation is a diagnosis and a fix. The stated
reason for hesitating (the file lived in a different repo's scratchpad) was real
but did not justify withholding the diagnosis.

---

## 2. Diagnosis and repair of `conread.ps1`

### The mechanism being repaired

Four calls in sequence: `FreeConsole()` to let go of any console currently held,
`AttachConsole(pid)` to join the target's console, `CreateFileW("CONOUT$", …)`
to get a handle on its active screen buffer, then `GetConsoleScreenBufferInfo`
and `ReadConsoleOutputCharacterW` to read the rows.

### Fault 1 — hex literal turns negative

`0x80000000` (GENERIC_READ) written as a hex literal. Windows PowerShell 5.1
parses whole-number literals as signed 32-bit, and that value is one past the
top of the range, so it arrived as negative 2,147,483,648 and the unsigned
parameter refused it.

Fixed by writing the decimal and casting: `[uint32]2147483648`. Casting the hex
literal does not help — it is already negative before the cast runs.

### Fault 2 — the unchecked handle

The three follow-on errors were not separate bugs. The handle was never issued,
the script never checked it, and each later call failed on the same empty value.
One cause, four messages. Fixed by testing for `INVALID_HANDLE_VALUE` (which is
-1, not zero) and bailing out with the Win32 error number.

### Fault 3 — `CONOUT$` needs read *and* write

The script asked for `GENERIC_READ` alone. Microsoft documents
`GENERIC_READ | GENERIC_WRITE` as the requirement. Changed to 3221225472.

### Fault 4 — the one that cost the most

With the first three fixed, the script stopped erroring and started *crashing*,
with no message at all. Two different crashes, found by adding a trace that
wrote progress to a file after every call:

```
20:31:25.947 attach returned True err=187
20:31:25.955 createfile handle=2972 err=0
20:31:25.969 bufferinfo ok=True size=120x30 cursor=52,3 err=203
20:31:25.973 w=120 rows=4
  → exit -1073741819  (0xC0000005, access violation)
```

First attempt at a fix — swapping `StringBuilder` for `[Out] char[]` — moved the
crash rather than curing it: `-1073740940` (`0xC0000374`, heap corruption). That
change of symptom was the clue. A different corruption, not a fixed one, means
the buffer is genuinely too small rather than merely mis-marshalled.

The real cause: the `DllImport` for `ReadConsoleOutputCharacterW` specified no
`CharSet`. .NET defaults to **ANSI**, one byte per character. The function is the
`...W` wide variant and writes **two**. Asking for 120 characters made Windows
write 240 bytes into a 120-byte buffer — an overrun of exactly double, every
call. `CharSet=CharSet.Unicode` on every `...W` import fixed it.

### Verification

Not "it runs" — a marker string was planted and had to come back:

```
buffer 120x30  cursor 52,5  rows_read 6
FINAL_CHECK_ALPHA
FINAL_CHECK_BRAVO

Microsoft Windows [Version 10.0.26200.9457]
```

The broken original was kept beside the fixed one as `conread.ps1.broken.bak`.

### A trap worth remembering

In the trace above, `AttachConsole` returned **True** while the error code read
187, and `GetConsoleScreenBufferInfo` returned **True** while it read 203. Both
calls succeeded. `GetLastError` is only meaningful after a call that reported
failure. Branch on the boolean, never on the error number.

---

## 3. The Whisper / AllTalk question, answered by experiment

The reason this work was wanted at all: repeated trouble getting this app to
attach to the Whisper and AllTalk consoles. The answer turned out to differ by
repo, and one half of it overturned an assumption.

### The kobold launcher — attachment can never work there

`REPO_koboldccp_sst_tts_media\launcher.py` (around line 522) starts every
service with `stdout=self._logf`, `stderr=subprocess.STDOUT` and no new console.
Those processes have no screen buffer at all. `AttachConsole` returns error 6,
`ERROR_INVALID_HANDLE`, and always will. Their output is already on disk under
`logs\`. Read the file; screen-scraping them is wasted effort.

### This app — a pseudoconsole, and it *can* be read

The hosting chain here is `tkwinterm.winterminal.Terminal` → `WinPtyHandler` →
`winpty.PtyProcess.spawn('cmd.exe')`, so each embedded pane is a ConPTY hosting
a real `cmd.exe`, into which `_start_whisper` and `_start_alltalk` type their
commands.

The standing assumption was that a window-less pseudoconsole cannot be read from
outside. **Tested rather than assumed, and it is wrong.** A `winpty.PtyProcess`
was spawned, a marker echoed into it, and the fixed script run against the PID it
reported:

```
buffer 120x30  cursor 52,6  rows_read 7
Microsoft Windows [Version 10.0.26200.9457]
(c) Microsoft Corporation. All rights reserved.

F:\...>echo PTY_MARKER_9999
PTY_MARKER_9999
```

A ConPTY exposes an ordinary screen buffer and attachment reaches it. The catch
is which process to name: **the PID `PtyProcess` reports**, which is the hosted
`cmd.exe`, not the Python process that spawned it.

Caveat stated plainly: this was proved with an equivalent `pywinpty` process,
**not** against a live Whisper or AllTalk pane, because neither service was
running (ports 8787 and 7851 both idle). The mechanism is the same; the specific
services remain unconfirmed.

### Why `_find_console_ancestor_pid` exists

`_graceful_shutdown_service` does not attach to the service process. It finds the
PID on the port, walks up to five levels of parent looking for `cmd.exe`, and
attaches to that — because `python server.py` never created the console, the
`cmd.exe` above it did, and `GenerateConsoleCtrlEvent` is delivered to a console.

Two details in that routine that are load-bearing and easy to delete by accident:
`SetConsoleCtrlHandler(None, True)` **before** sending Ctrl+C, or it kills this
process too; and `FreeConsole()` afterwards, paired with
`SetConsoleCtrlHandler(None, False)`.

Likewise, `_write_console_keys` declares `argtypes` and `restype` on its
`ctypes` calls before invoking them. That is the `ctypes` equivalent of getting
the `DllImport` signature right — the same fault-4 class of silent memory
corruption is available in Python without it.

---

## 4. Documentation written

- `REPO_koboldccp_sst_tts_media\docs\console_attach_and_read.md` — the full
  write-up: vocabulary from scratch, the four-step recipe, each fault with its
  real error text, the complete working script, a failure-code table, and an
  explicit list of what was and was not tested.
  Commit `6d86931`.
- `REPO_claude_code_voice_mode\logs\2026-09-19_console_attach_and_read.md` —
  the voice-side summary, focused on what the four faults and the ConPTY finding
  mean for `mic_panel.py`. Commit `1fa7079`.

The script printed inside the kobold doc was extracted back out of the markdown
and run against a live console before the doc was committed, so the documented
copy is verified rather than merely transcribed.

---

## 5. Logging moved out of the shared folder

### What was writing where

Three files in this repo wrote into `F:\Apps\freedom_system\log`, and nothing
else did:

| File | Wrote |
|---|---|
| `claude_code_voice_mode_mcp_server.py` line 40 | `claude_code_voice_mode.log` |
| `mic_panel.py` line 61 | `claude_code_voice_mode_mic_panel.log` |
| `.claude\hooks\speak_on_stop.py` line 14 | same MCP log, as a failure fallback |

A repo-wide search turned up three other mentions of that path — a work log in
the kobold repo and two standards documents — all prose, not writers.

### The change

All three now derive the folder from their own file location rather than a
hardcoded drive path, so the logs follow the repo:

```python
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "claude_code_voice_mode.log"
```

and the hook uses its existing `PROJECT_DIR` constant. `.gitignore` gained
`logs/*.log*` so runtime logs and the history file stay local while written
notes in `logs/` stay tracked. All three files compile. Commit `724644d`.

### Verified with real traffic

Not a synthetic test. The old shared file's final entry is timestamped
**23:16:10**; the new file's entries begin at **23:17:48** and continue. Nothing
was written to the old path after the change.

### Moving the old files

- `claude_code_voice_mode_mic_panel.log` (82,677 bytes) — moved cleanly, nothing
  held it.
- `claude_code_voice_mode.log` (6,685,651 bytes) — **move refused**: "The
  process cannot access the file because it is being used by another process."
  Two MCP server processes held it open. Its history was copied out first (a
  locked file can still be read), so nothing was ever at risk.

### The point where the approved plan stopped being true

The restart had been approved on the understanding that it would briefly
interrupt this session's own voice. Checking the two processes showed something
different: PIDs 33832 and 29972, parented by `claude.exe`, started on 2026-09-16
and 2026-09-17 — they belonged to **other Claude Code sessions**, and one of
those sessions was actively in use (its stop hook wrote into the new log at
23:17, mid-conversation about unrelated work).

It also turned out the restart was no longer needed for the reroute at all: the
stop hook spawns its own Python process per response and imports `speak_text`
directly, so it had already picked up the new path without anything restarting.

Because the justification given for the restart no longer held, and the cost had
landed on a different session than described, the work stopped there and the
question was put again rather than decided. Re-approved explicitly, then:

- PID 29972 had already exited on its own.
- PID 33832 was stopped, the file released, and moved to
  `logs\claude_code_voice_mode.log.history`, byte count matched before and after
  (6,685,651).

### Final state

Shared folder — no voice-app files remain. What is left is `whisper_stt.log`,
four old `trace_*.log` files and three `Plan_for_*.txt` files, none of which this
repo writes.

Repo `logs\` folder:

```
2026-09-19_console_attach_and_read.md      5,216   (tracked)
2026-09-19_session_log.md                          (tracked - this file)
claude_code_voice_mode.log                55,024   (ignored, live)
claude_code_voice_mode.log.history     6,685,651   (ignored, moved history)
claude_code_voice_mode_mic_panel.log      82,677   (ignored, moved)
```

`git check-ignore` confirms the 6.6 MB history is ignored; the working tree is
clean.

---

## 6. Unrelated finding, and the most important line here

**Text-to-speech is failing, and has been throughout this session.** It is
nothing to do with the logging change — it was already failing before anything
was touched.

Every attempt dies against AllTalk on port 7851:

```
[2026-09-19 23:50:29] [INFO] [NONSTREAM] Trying native endpoint (Method 3)
[2026-09-19 23:50:31] [ERROR] [NONSTREAM] Method 3 failed: ... port=7851 ...
    [WinError 10061] No connection could be made because the target machine
    actively refused it
[2026-09-19 23:50:31] [ERROR] [STOP-HOOK] speak_text failed: All non-streaming
    TTS methods failed
```

Streaming first, then both non-streaming fallbacks, all three refused. AllTalk is
not running — consistent with the earlier check that found ports 8787 and 7851
both idle. Nothing was done about this; it is recorded so it is not lost.

---

## 7. Commits

| Repo | Commit | Subject |
|---|---|---|
| kobold | `6d86931` | Write down how to read another program's console window |
| voice | `1fa7079` | Start a logs folder, with the console-attach findings in it |
| voice | `724644d` | Keep this app's logs in this repo instead of the shared folder |

Note: two later commits from a concurrent session sit on top of `6d86931` in the
kobold repo (`0e6548f`, `3e5f16c`, both about face_training moving out). They are
unrelated to this work; `6d86931` is intact beneath them.

---

## 8. What was verified, and what was not

**Verified by running it:** all four fixes, against a real `cmd /k` console and
against a `pywinpty` ConPTY — marker text returned verbatim, exit code 0 in both
cases. The script exactly as printed in the kobold doc, extracted from the
markdown and re-run. The log reroute, by comparing timestamps across the old and
new files under real traffic. The file move, by byte count before and after. The
ignore rule, by `git check-ignore`.

**Not verified:** reading a live Whisper or AllTalk pane — neither service was
running. The mic panel's own logging path was changed but never exercised, since
the mic panel was not running either.

**Established by reading code, not by running it:** the `launcher.py`
redirection behaviour, and the `mic_panel.py` shutdown, injection and
console-ancestor routines.
