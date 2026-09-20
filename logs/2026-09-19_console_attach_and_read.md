# 2026-09-19 — Attaching to another program's console, and what it means here

Short version for this repo: the technique this app uses to reach into other
programs' consoles was broken in a helper script, all four faults are now found
and fixed, and one long-standing assumption about Whisper and AllTalk turned out
to be **wrong in our favour**.

The full write-up, with the complete working script and the vocabulary explained
from scratch, lives in the kobold repo at
`REPO_koboldccp_sst_tts_media\docs\console_attach_and_read.md`. This note is the
voice-side summary.

---

## Why this matters to the voice app

`mic_panel.py` leans on console attachment in three places:

- `_inject_text_into_terminal` — `AttachConsole`, then `WriteConsoleInputW` into
  `CONIN$`, to type transcribed speech into a Claude Code terminal.
- `_attach_and_send_ctrl_c` — `AttachConsole`, then `GenerateConsoleCtrlEvent`,
  to shut Whisper and AllTalk down politely.
- `_find_console_ancestor_pid` — walks up to five levels of parent process
  looking for `cmd.exe`, because the service process never owned the console.

All three are the same mechanism, and all three are easy to get subtly wrong.

---

## The four faults found (in a PowerShell helper, 2026-09-19)

1. **Hex flag goes negative.** `0x80000000` written as a hex literal in Windows
   PowerShell 5.1 is parsed as a *signed* 32-bit integer, so it wraps to
   negative 2,147,483,648 and the unsigned parameter refuses it. Write the
   decimal and cast: `[uint32]2147483648`. Casting the hex literal does not
   help — it is already negative by then.
   *This one is PowerShell-only.* `mic_panel.py` writes the same constants as
   `0x80000000 | 0x40000000` in Python and is fine, because Python integers have
   no fixed width.

2. **Unchecked handle.** A failed `CreateFileW` returns `INVALID_HANDLE_VALUE`
   (which is -1, not zero). The script never checked, so one failure produced
   four cascading error messages and hid its own cause.

3. **`CONOUT$` needs read *and* write.** Opening it with `GENERIC_READ` alone is
   not enough; Microsoft documents `GENERIC_READ | GENERIC_WRITE`.

4. **ANSI/Unicode mismatch — the expensive one.** The `DllImport` for
   `ReadConsoleOutputCharacterW` had no `CharSet`, so .NET defaulted to ANSI and
   reserved **one byte per character** while the `...W` function wrote **two**.
   Every call overran its buffer by exactly double. The result was not an error
   message but a hard crash: `0xC0000005` (access violation) with a
   `StringBuilder`, `0xC0000374` (heap corruption) with a `char[]`.
   Fix: `CharSet=CharSet.Unicode` on every `...W` import, and prefer
   `[Out] char[]` over `StringBuilder` for the read.

**Relevant to this repo:** fault 4 has a direct `ctypes` equivalent. If the
`argtypes` and `restype` are not declared before calling a `...W` function, the
same silent memory corruption is available in Python. `mic_panel.py` currently
*does* declare them in `_write_console_keys` — that is load-bearing, not
decoration, and should not be trimmed.

**Time-waster worth knowing:** `GetLastError` is only meaningful after a call
that reported failure. During tracing, `AttachConsole` returned True while the
error code read 187, and `GetConsoleScreenBufferInfo` returned True while it read
203. Both had succeeded. Always branch on the boolean return.

---

## The Whisper / AllTalk finding

The assumption had been that the embedded Whisper and AllTalk panes cannot be
read from outside, because they are pseudoconsoles with no window. **That is
wrong, and it was tested rather than assumed.**

The hosting chain is `tkwinterm.winterminal.Terminal` → `WinPtyHandler` →
`winpty.PtyProcess.spawn('cmd.exe')`, so each pane is a ConPTY hosting a real
`cmd.exe`. Spawning an equivalent `PtyProcess`, echoing a marker into it, and
attaching to the PID it reports returned the marker verbatim, with a normal
120×30 screen buffer. So a ConPTY exposes an ordinary screen buffer and
`AttachConsole` plus `CONOUT$` reaches it.

The catch: attach to **the PID `PtyProcess` reports** — that is the hosted
`cmd.exe`, not the Python process that spawned it.

Caveat, stated plainly: this was proved with an equivalent `pywinpty` process,
**not** with a live Whisper or AllTalk pane. Neither service was running at the
time (ports 8787 and 7851 were both idle). The mechanism is the same; the
specific services are unconfirmed.

**When to actually use it.** Inside the mic panel, don't. `WinPtyHandler` is
already streaming that text through `pty_process.read()` into a `pyte` screen
and on into the Tk widget — read it from there. The attach technique earns its
place when something *outside* the mic panel needs to see a pane's contents.

---

## Contrast worth remembering: the kobold launcher

`REPO_koboldccp_sst_tts_media\launcher.py` starts its services with
`stdout=self._logf` and no new console. Those processes have no screen buffer at
all, so attaching to them can never work — `AttachConsole` returns error 6,
`ERROR_INVALID_HANDLE`. Their output is already on disk under `logs\`. Read the
file.

Same two service names, opposite correct answer, depending on which repo
launched them.
