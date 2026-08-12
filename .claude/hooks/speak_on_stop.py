"""
Stop hook: reads Claude's last message from stdin JSON, strips code blocks
and markdown formatting, and speaks whatever prose/comments/summary remains.
Reuses the MCP server's own speak_text() so voice/logging stay consistent.
"""
import datetime
import json
import re
import sys
import traceback
from pathlib import Path

PROJECT_DIR = r"F:\Apps\freedom_system\REPO_claude_code_voice_mode"
FALLBACK_LOG = Path(r"F:\Apps\freedom_system\log\claude_code_voice_mode.log")


def _log_failure(stage: str, exc: Exception):
    # Written directly, independent of the MCP server's logging setup,
    # so a failure here is never silent even if the import itself failed.
    try:
        FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with FALLBACK_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[CLAUDE_CODE_VOICE_MODE] [{ts}] [ERROR] [STOP-HOOK] {stage} failed: {exc}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass  # nothing more we can do without a filesystem


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # fenced code blocks
    text = re.sub(r"`[^`]*`", " ", text)  # inline code spans
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)  # bold
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)  # italics
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # headers
    text = re.sub(r"^[-*]\s+", "", text, flags=re.MULTILINE)  # bullet markers
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _log_failure("stdin/JSON parse", e)
        return

    text = data.get("last_assistant_message") or ""
    cleaned = strip_code(text)
    if len(cleaned) < 3:
        return

    sys.path.insert(0, PROJECT_DIR)
    try:
        from claude_code_voice_mode_mcp_server import speak_text
        result = speak_text(cleaned)
        if isinstance(result, dict) and result.get("status") == "error":
            _log_failure("speak_text", Exception(result.get("message", "unknown TTS error")))
    except Exception as e:
        _log_failure("import/speak_text call", e)


if __name__ == "__main__":
    main()
