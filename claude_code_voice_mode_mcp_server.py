"""
Claude Code Voice Mode MCP Server
Custom MCP server providing voice I/O for Claude Code.
- speak(text): Sends text to AllTalk TTS, plays audio
- listen(): Captures mic audio, sends to Whisper STT, returns text
- converse(message): Full loop - speak message, listen for response
- set_voice(voice): Changes AllTalk voice
"""
import asyncio
import io
import json
import logging
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import sounddevice as sd
from mcp.server import Server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ALLTALK_URL = "http://127.0.0.1:7851"
WHISPER_URL = "http://127.0.0.1:8787"
DEFAULT_VOICE = "Freya.wav"
PLAYBACK_SPEED = 1.0  # 0.5 = half speed, 1.0 = normal, 2.0 = double speed
SAMPLE_RATE = 16000
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher = more aggressive filtering
STREAMING_CHUNK_SIZE = 4096  # bytes per iter_content chunk
WAV_HEADER_SIZE = 44  # standard WAV header

LOG_FILE = Path("F:/Apps/freedom_system/log/claude_code_voice_mode.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[CLAUDE_CODE_VOICE_MODE] [%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
current_voice = DEFAULT_VOICE
current_speed = 1.0  # Playback speed (0.5-2.0)
current_temperature = 1.1  # TTS generation temperature (0.1-1.5)
mic_muted = False
_streaming_available: Optional[bool] = None  # None = not yet checked
mic_mode = "push_to_talk"  # push_to_talk, toggle
_services_available: bool = False
_voice_mode_disabled: bool = False
STATE_FILE = Path("F:/Apps/freedom_system/REPO_claude_code_voice_mode/mic_state.json")


def _get_input_device_index() -> Optional[int]:
    """Read input_device from mic_state.json and resolve to a sounddevice index.

    Returns the device index if a specific device is selected, or None to use
    the system default.
    """
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        device_name = state.get("input_device")
        if not device_name:
            return None
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and d["name"] == device_name:
                logger.info(f"Using input device #{i}: {device_name}")
                return i
        logger.warning(f"Input device '{device_name}' not found, using default")
        return None
    except Exception as e:
        logger.warning(f"Could not read input device from state: {e}")
        return None


def get_playback_speed() -> float:
    """Return the current playback speed."""
    return current_speed


def is_tts_paused() -> bool:
    """Check if TTS is paused by reading the shared state file."""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state.get("tts_paused", False)
    except Exception:
        return False


def _wait_for_playback_or_pause():
    """Wait for audio playback to complete, or stop if TTS is paused."""
    while True:
        try:
            stream = sd.get_stream()
            if not stream.active:
                break
        except RuntimeError:
            break
        if is_tts_paused():
            sd.stop()
            logger.info("Playback stopped: TTS paused")
            break
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def numpy_to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert numpy float32 audio array to WAV bytes."""
    audio_int16 = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def record_audio(duration: float = 5.0, silence_timeout: float = 2.0) -> np.ndarray:
    """
    Record audio from microphone with Voice Activity Detection.
    Stops when silence is detected for silence_timeout seconds,
    or when max duration is reached.
    """
    import webrtcvad

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    frame_duration_ms = 30  # ms per VAD frame
    frame_size = int(SAMPLE_RATE * frame_duration_ms / 1000)
    max_frames = int(duration * SAMPLE_RATE / frame_size)
    silence_frames_threshold = int(silence_timeout * 1000 / frame_duration_ms)

    device_index = _get_input_device_index()
    logger.info(
        f"Recording... (max {duration}s, silence timeout {silence_timeout}s, "
        f"device={'default' if device_index is None else device_index})"
    )

    all_frames = []
    silence_count = 0
    speech_detected = False

    def audio_callback(indata, frames, time_info, status):
        nonlocal silence_count, speech_detected
        if status:
            logger.warning(f"Audio status: {status}")
        if mic_muted:
            return
        all_frames.append(indata.copy())

    stream_kwargs = {
        "samplerate": SAMPLE_RATE,
        "channels": CHANNELS,
        "dtype": "int16",
        "blocksize": frame_size,
        "callback": audio_callback,
    }
    if device_index is not None:
        stream_kwargs["device"] = device_index

    with sd.InputStream(**stream_kwargs):
        for _ in range(max_frames):
            sd.sleep(frame_duration_ms)
            if not all_frames:
                continue

            # Check last frame for voice activity
            last_frame = all_frames[-1].flatten().tobytes()
            if len(last_frame) == frame_size * 2:  # 2 bytes per int16 sample
                is_speech = vad.is_speech(last_frame, SAMPLE_RATE)
                if is_speech:
                    speech_detected = True
                    silence_count = 0
                else:
                    silence_count += 1

                # Stop if we had speech and now have enough silence
                if speech_detected and silence_count >= silence_frames_threshold:
                    logger.info("Silence detected, stopping recording.")
                    break

    if not all_frames:
        return np.array([], dtype=np.float32)

    audio = np.concatenate(all_frames).flatten().astype(np.float32) / 32768.0
    logger.info(f"Recorded {len(audio) / SAMPLE_RATE:.1f}s of audio")
    return audio


def play_audio_from_url(url: str):
    """Download audio from URL and play it through speakers."""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        # Write to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        import av
        container = av.open(tmp_path)
        audio_stream = next(s for s in container.streams if s.type == "audio")

        frames = []
        base_rate = 24000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=base_rate)
        for frame in container.decode(audio_stream):
            resampled = resampler.resample(frame)
            for r in resampled:
                frames.append(r.to_ndarray().flatten())

        if frames:
            audio = np.concatenate(frames).astype(np.float32) / 32768.0
            playback_sr = int(base_rate * get_playback_speed())
            sd.play(audio, samplerate=playback_sr)
            _wait_for_playback_or_pause()

        Path(tmp_path).unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Audio playback failed: {e}")


def play_audio_bytes(audio_bytes: bytes):
    """Play raw audio bytes (WAV format) through speakers."""
    try:
        buf = io.BytesIO(audio_bytes)
        import av
        container = av.open(buf)
        audio_stream = next(s for s in container.streams if s.type == "audio")

        frames = []
        target_rate = 24000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
        for frame in container.decode(audio_stream):
            resampled = resampler.resample(frame)
            for r in resampled:
                frames.append(r.to_ndarray().flatten())

        if frames:
            audio = np.concatenate(frames).astype(np.float32) / 32768.0
            playback_sr = int(target_rate * get_playback_speed())
            sd.play(audio, samplerate=playback_sr)
            _wait_for_playback_or_pause()
    except Exception as e:
        logger.error(f"Audio playback failed: {e}")


# ---------------------------------------------------------------------------
# TTS: Send text to AllTalk
# ---------------------------------------------------------------------------
def _parse_wav_header(header: bytes) -> Optional[dict]:
    """Parse a 44-byte WAV header and return audio format info."""
    if len(header) < WAV_HEADER_SIZE:
        return None
    try:
        riff, size, wave_id = struct.unpack_from("<4sI4s", header, 0)
        if riff != b"RIFF" or wave_id != b"WAVE":
            return None
        # fmt chunk starts at offset 12
        fmt_id, fmt_size, audio_fmt, channels, sample_rate = struct.unpack_from(
            "<4sIHHI", header, 12
        )
        byte_rate, block_align, bits_per_sample = struct.unpack_from(
            "<IHH", header, 28
        )
        return {
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": bits_per_sample,
            "block_align": block_align,
        }
    except struct.error:
        return None


class StreamingStallError(Exception):
    """Raised when streaming TTS stalls beyond calculated limits."""
    pass


def speak_text_streaming(text: str, voice: str) -> dict:
    """Stream audio from AllTalk's streaming endpoint for low-latency playback."""
    global _streaming_available

    params = {
        "text": text,
        "voice": voice,
        "language": "en",
        "output_file": "streaming_output.wav",
        "temperature": str(current_temperature),
    }
    response = requests.get(
        f"{ALLTALK_URL}/api/tts-generate-streaming",
        params=params,
        stream=True,
        timeout=30,
    )
    response.raise_for_status()

    # Use a SINGLE iter_content() call for both header and audio data.
    # Calling iter_content() twice on the same Response yields nothing
    # on the second call (requests library behavior).
    raw_iter = response.iter_content(chunk_size=STREAMING_CHUNK_SIZE)

    # Accumulate WAV header from the first bytes of the stream
    header = b""
    for chunk in raw_iter:
        header += chunk
        if len(header) >= WAV_HEADER_SIZE:
            break

    leftover = header[WAV_HEADER_SIZE:]
    header = header[:WAV_HEADER_SIZE]

    fmt = _parse_wav_header(header)
    if fmt is None:
        response.close()
        raise ValueError("Invalid WAV header from streaming endpoint")

    sr = fmt["sample_rate"]
    ch = fmt["channels"]
    dtype = "int16" if fmt["bits_per_sample"] == 16 else "int32"
    frame_size = fmt["block_align"]  # bytes per frame (channels * bytes_per_sample)

    # Apply playback speed by adjusting output sample rate
    speed = get_playback_speed()
    playback_sr = int(sr * speed)
    logger.info(
        f"Streaming TTS: {sr}Hz, {ch}ch, {fmt['bits_per_sample']}bit, speed={speed}x (playback_sr={playback_sr}Hz)"
    )

    stream = sd.RawOutputStream(
        samplerate=playback_sr, channels=ch, dtype=dtype
    )
    stream.start()
    logger.info(
        f"[STREAM-A] RawOutputStream opened: device={sd.default.device[1]}, "
        f"sr={sr}, ch={ch}, dtype={dtype}, latency={stream.latency}"
    )

    remainder = leftover
    total_bytes_written = 0
    chunk_count = 0
    first_chunk_logged = False
    t_start = time.monotonic()
    t_first_write = None
    exit_reason = "exhausted"  # "exhausted", "paused", "write_error", "stall"

    try:
        last_chunk_time = time.monotonic()

        for chunk in raw_iter:  # Same iterator — continues after header
            now = time.monotonic()
            chunk_gap = now - last_chunk_time
            last_chunk_time = now

            # AllTalk delivers chunks continuously. A gap this long means
            # the generation pipeline has stalled. This threshold is based on
            # observed AllTalk behavior: 1-5s total generation, chunks every ~10ms.
            # A 10s gap is far beyond normal and indicates a real problem.
            if chunk_gap > 10.0 and chunk_count > 0:
                logger.warning(
                    f"[STREAM-STALL] No chunk for {chunk_gap:.1f}s "
                    f"(after {chunk_count} chunks, {total_bytes_written} bytes). "
                    f"Treating as stall."
                )
                exit_reason = "stall"
                break

            if is_tts_paused():
                logger.info("Streaming playback stopped: TTS paused")
                exit_reason = "paused"
                break

            data = remainder + chunk
            # Align to frame boundary
            usable = len(data) - (len(data) % frame_size)
            if usable > 0:
                try:
                    stream.write(data[:usable])
                    total_bytes_written += usable
                    chunk_count += 1
                    if not first_chunk_logged:
                        t_first_write = time.monotonic()
                        first_chunk_logged = True
                        logger.info(
                            f"[STREAM-B] First chunk written: {usable} bytes "
                            f"(time_to_first_audio={t_first_write - t_start:.3f}s)"
                        )
                except sd.PortAudioError as e:
                    logger.error(
                        f"[STREAM-E] stream.write() failed: {e} "
                        f"(after {total_bytes_written} bytes, {chunk_count} chunks)"
                    )
                    exit_reason = "write_error"
                    break
            remainder = data[usable:]

        # Write any final remainder (should be frame-aligned if stream is well-formed)
        if remainder and not is_tts_paused():
            # Pad to frame boundary if needed
            pad = frame_size - (len(remainder) % frame_size)
            if pad < frame_size:
                remainder += b"\x00" * pad
            try:
                stream.write(remainder)
                total_bytes_written += len(remainder)
            except sd.PortAudioError as e:
                logger.error(f"[STREAM-E] Final stream.write() failed: {e}")

        t_loop_end = time.monotonic()
        logger.info(
            f"[STREAM-C] Iter loop done: {chunk_count} chunks, {total_bytes_written} bytes, "
            f"reason={exit_reason}, loop_duration={t_loop_end - (t_first_write or t_start):.3f}s"
        )

        # If no audio data came through, raise so fallback can handle it
        if total_bytes_written == 0:
            exit_reason = "no_audio"
            raise ValueError("Streaming returned WAV header but 0 bytes of audio data")

    finally:
        # Calculate remaining playback from device output latency.
        # stream.write() blocks until consumed, so the output buffer
        # holds at most stream.latency seconds of unplayed audio.
        try:
            output_latency = stream.latency
            if isinstance(output_latency, tuple):
                output_latency = output_latency[1]  # (input_latency, output_latency)
        except Exception:
            output_latency = 0.2  # safe fallback for Windows audio

        remaining_playback = output_latency + 0.5

        logger.info(
            f"[STREAM-D] Drain: device_latency={output_latency:.3f}s, "
            f"calculated_wait={remaining_playback:.3f}s"
        )

        # Wait the calculated time, checking for pause every 50ms
        drain_end = time.monotonic() + remaining_playback
        while time.monotonic() < drain_end:
            if is_tts_paused():
                logger.info("[STREAM-D] Drain interrupted: TTS paused")
                break
            time.sleep(0.05)

        stall_detected = exit_reason == "stall"
        if stall_detected:
            logger.warning("[STREAM-D] Stall confirmed: streaming will be disabled")

        stream.stop()
        stream.close()
        response.close()

        t_end = time.monotonic()
        logger.info(
            f"[STREAM-D] Complete: {total_bytes_written} bytes, {chunk_count} chunks, "
            f"wall={t_end - t_start:.3f}s, "
            f"audio_dur={total_bytes_written / (sr * frame_size) if sr and frame_size else 0:.3f}s"
        )

        if stall_detected:
            raise StreamingStallError(
                f"Streaming stalled after {chunk_count} chunks, "
                f"{total_bytes_written} bytes"
            )

    _streaming_available = True
    return {"status": "spoken", "method": "streaming", "voice": voice, "length": len(text)}


def speak_text_nonstreaming(text: str, voice: Optional[str] = None) -> dict:
    """Speak using non-streaming methods only (Methods 2/3). Used for recovery."""
    voice = voice or current_voice

    # Method 2: OpenAI-compatible endpoint
    try:
        logger.info("[NONSTREAM] Trying OpenAI endpoint (Method 2)")
        response = requests.post(
            f"{ALLTALK_URL}/v1/audio/speech",
            json={"input": text, "voice": voice, "model": "tts-1", "response_format": "wav"},
            timeout=30,
        )
        if response.status_code == 200:
            play_audio_bytes(response.content)
            logger.info(f"[NONSTREAM] Method 2 success: {len(response.content)} bytes")
            return {"status": "spoken", "method": "nonstreaming_openai", "voice": voice, "length": len(text)}
    except Exception as e:
        logger.warning(f"[NONSTREAM] Method 2 failed: {e}")

    # Method 3: AllTalk native endpoint
    try:
        logger.info("[NONSTREAM] Trying native endpoint (Method 3)")
        payload = {
            "text_input": text,
            "text_filtering": "standard",
            "character_voice_gen": voice,
            "narrator_enabled": "false",
            "narrator_voice_gen": "",
            "text_not_inside": "character",
            "language": "en",
            "output_file_name": "claude_code_voice_mode",
            "output_file_timestamp": "true",
            "autoplay": "false",
            "autoplay_volume": "0.8",
            "speed": "0.25",
            "pitch": "1.0",
            "temperature": str(current_temperature),
            "repetition_penalty": "10.0",
        }
        response = requests.post(f"{ALLTALK_URL}/api/tts-generate", data=payload, timeout=30)
        if response.status_code == 200:
            result = response.json()
            audio_url = result.get("output_file_url", "")
            if audio_url:
                if not audio_url.startswith("http"):
                    audio_url = f"{ALLTALK_URL}{audio_url}"
                play_audio_from_url(audio_url)
                logger.info(f"[NONSTREAM] Method 3 success: url={audio_url}")
                return {"status": "spoken", "method": "nonstreaming_native", "voice": voice, "length": len(text)}
    except Exception as e:
        logger.error(f"[NONSTREAM] Method 3 failed: {e}")

    return {"status": "error", "message": "All non-streaming TTS methods failed"}


def _handle_streaming_failure(original_text: str, voice: str) -> dict:
    """Handle streaming failure: notify user via non-streaming, verify audio, offer fix."""
    global _voice_mode_disabled

    logger.info("[RECOVERY] Starting streaming failure recovery")

    # Inform user via TTS using non-streaming mode
    notify_result = speak_text_nonstreaming(
        "Streaming audio had a problem. I've switched to regular mode. Can you hear me?",
        voice,
    )

    if notify_result.get("status") != "spoken":
        # Non-streaming also failed — total audio failure
        logger.error("[RECOVERY] Non-streaming notification also failed")
        _voice_mode_disabled = True
        return {
            "status": "error",
            "voice_mode_disabled": True,
            "message": "Audio playback completely failed (both streaming and non-streaming). Voice mode disabled.",
            "action": "BEGIN_DMAIC_TROUBLESHOOTING",
            "instruction": (
                "TOTAL AUDIO FAILURE — both streaming and non-streaming playback failed. "
                "Voice mode disabled. Communicate via text only. BEGIN DMAIC troubleshooting "
                "NOW. Read the DMAIC process at "
                "F:/Apps/freedom_system/standards/DMAIC_coding_process_tool.md "
                "and follow it systematically. Check AllTalk TTS service, audio output "
                "device, sounddevice configuration, and speaker connections."
            ),
        }

    # Listen for user response
    logger.info("[RECOVERY] Listening for user confirmation...")
    try:
        audio = record_audio(duration=8.0, silence_timeout=3.0)
        if len(audio) > 0:
            response_text = transcribe_audio(audio).strip().lower()
            logger.info(f"[RECOVERY] User response: '{response_text}'")

            # If user confirms hearing — offer to fix streaming
            yes_words = ["yes", "yeah", "yep", "yup", "hear", "ok", "okay", "can", "working"]
            if any(word in response_text for word in yes_words):
                logger.info("[RECOVERY] User confirmed audio works. Speaking original text.")
                speak_text_nonstreaming(original_text, voice)
                speak_text_nonstreaming(
                    "Good, you can hear me. I'm going to start troubleshooting the streaming issue now.",
                    voice,
                )
                return {
                    "status": "spoken_with_recovery",
                    "streaming_disabled": True,
                    "user_confirmed_audio": True,
                    "length": len(original_text),
                    "action": "BEGIN_DMAIC_TROUBLESHOOTING",
                    "instruction": (
                        "Streaming audio stalled but non-streaming audio works. "
                        "The user confirmed they can hear. BEGIN DMAIC troubleshooting "
                        "of the streaming TTS issue NOW. Read the DMAIC process at "
                        "F:/Apps/freedom_system/standards/DMAIC_coding_process_tool.md "
                        "and follow it systematically. The known root cause is the "
                        "infinite drain loop in speak_text_streaming() — "
                        "while stream.active never becomes False for sd.RawOutputStream "
                        "without a callback. Notify the user via speak that you are "
                        "beginning DMAIC troubleshooting."
                    ),
                }

            # If user says no — disable voice mode
            no_words = ["no", "nope", "can't", "cannot", "nothing", "don't"]
            if any(word in response_text for word in no_words):
                logger.warning("[RECOVERY] User cannot hear audio. Disabling voice mode.")
                _voice_mode_disabled = True
                return {
                    "status": "error",
                    "voice_mode_disabled": True,
                    "message": "User confirmed they cannot hear audio. Voice mode disabled.",
                    "action": "BEGIN_DMAIC_TROUBLESHOOTING",
                    "instruction": (
                        "The user CANNOT hear any audio (streaming or non-streaming). "
                        "Voice mode is now disabled — communicate via text only. "
                        "BEGIN DMAIC troubleshooting NOW. Read the DMAIC process at "
                        "F:/Apps/freedom_system/standards/DMAIC_coding_process_tool.md "
                        "and follow it systematically. The problem is broader than "
                        "just the streaming drain loop — non-streaming audio also "
                        "failed. Investigate audio output device, AllTalk TTS service, "
                        "and sounddevice configuration."
                    ),
                }

        # No response or unclear response
        logger.info("[RECOVERY] No clear response from user")
    except Exception as e:
        logger.error(f"[RECOVERY] Listen/transcribe failed: {e}")

    # Unclear response — speak original text anyway (in case they CAN hear)
    # and return with unclear status. Still start DMAIC either way.
    speak_text_nonstreaming(original_text, voice)
    return {
        "status": "spoken_with_recovery",
        "streaming_disabled": True,
        "user_response_unclear": True,
        "length": len(original_text),
        "action": "BEGIN_DMAIC_TROUBLESHOOTING",
        "instruction": (
            "Streaming failed. Switched to regular mode and spoke the message, "
            "but could not confirm the user heard it. BEGIN DMAIC troubleshooting "
            "NOW. Read the DMAIC process at "
            "F:/Apps/freedom_system/standards/DMAIC_coding_process_tool.md "
            "and follow it systematically. First ask the user (via text) if they "
            "can hear, then proceed with troubleshooting."
        ),
    }


def speak_text(text: str, voice: Optional[str] = None) -> dict:
    """Send text to AllTalk TTS and play the result.

    Tries streaming first, then falls back to non-streaming methods.
    Catches StreamingStallError to trigger recovery flow.
    """
    global _streaming_available, _voice_mode_disabled

    # If voice mode was disabled by recovery handler, return immediately
    if _voice_mode_disabled:
        return {
            "status": "error",
            "voice_mode_disabled": True,
            "message": "Voice mode is disabled because audio was not working. Communicate via text.",
        }

    if is_tts_paused():
        logger.info("TTS is paused, skipping speech")
        return {"status": "paused", "message": "TTS is currently paused via mic panel"}

    voice = voice or current_voice
    logger.info(f"Speaking: '{text[:80]}...' with voice={voice}")

    # Method 1: Streaming (fastest — plays audio as it's generated)
    if _streaming_available is not False:
        try:
            return speak_text_streaming(text, voice)
        except StreamingStallError as e:
            logger.error(f"Streaming stalled: {e}")
            _streaming_available = False
            return _handle_streaming_failure(text, voice)
        except Exception as e:
            if _streaming_available is None:
                logger.info(f"Streaming not available, disabling: {e}")
                _streaming_available = False
            else:
                logger.warning(f"Streaming failed (transient), falling back: {e}")

    # Methods 2/3 via non-streaming function
    return speak_text_nonstreaming(text, voice)


# ---------------------------------------------------------------------------
# STT: Send audio to Whisper
# ---------------------------------------------------------------------------
def transcribe_audio(audio: np.ndarray) -> str:
    """Send audio to Whisper STT and return transcribed text."""
    wav_bytes = numpy_to_wav_bytes(audio)
    logger.info(f"Sending {len(wav_bytes)} bytes to Whisper STT...")

    try:
        response = requests.post(
            f"{WHISPER_URL}/v1/audio/transcriptions",
            files={"file": ("recording.wav", wav_bytes, "audio/wav")},
            data={"model": "whisper-1", "language": "en"},
            timeout=30,
        )
        if response.status_code == 200:
            result = response.json()
            text = result.get("text", "").strip()
            logger.info(f"Transcribed: '{text}'")
            return text
        else:
            logger.error(f"Whisper returned {response.status_code}: {response.text}")
            return ""
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
server = Server("claude-code-voice-mode")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="speak",
            description="Convert text to speech using AllTalk TTS and play through speakers. Use this to read responses aloud.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to speak aloud",
                    },
                    "voice": {
                        "type": "string",
                        "description": f"Voice to use (default: {DEFAULT_VOICE})",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="listen",
            description="Record audio from microphone and transcribe using Whisper STT. Returns the transcribed text. Use this when the user wants to speak instead of type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "duration": {
                        "type": "number",
                        "description": "Max recording duration in seconds (default: 10)",
                    },
                    "silence_timeout": {
                        "type": "number",
                        "description": "Stop after this many seconds of silence (default: 2)",
                    },
                },
            },
        ),
        Tool(
            name="converse",
            description="Full voice conversation: speak a message through TTS, then listen for the user's spoken response via STT. Returns the user's transcribed speech.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Message to speak before listening",
                    },
                    "listen_duration": {
                        "type": "number",
                        "description": "Max listen duration in seconds (default: 10)",
                    },
                },
                "required": ["message"],
            },
        ),
        Tool(
            name="set_voice",
            description="Change the TTS voice used by AllTalk. List available voices at http://127.0.0.1:7851/api/voices",
            inputSchema={
                "type": "object",
                "properties": {
                    "voice": {
                        "type": "string",
                        "description": "Voice filename (e.g., 'Freya.wav', 'Arnold.wav')",
                    },
                },
                "required": ["voice"],
            },
        ),
        Tool(
            name="set_speed",
            description="Change the TTS playback speed. Adjusts sample rate for faster/slower speech. Range: 0.5 (half speed) to 2.0 (double speed). Default: 1.0.",
            inputSchema={
                "type": "object",
                "properties": {
                    "speed": {
                        "type": "number",
                        "description": "Playback speed multiplier (0.5-2.0, default 1.0)",
                    },
                },
                "required": ["speed"],
            },
        ),
        Tool(
            name="set_temperature",
            description="Change the TTS generation temperature. Higher values produce more varied/expressive speech, lower values are more consistent. Range: 0.1 to 1.5. Default: 0.75.",
            inputSchema={
                "type": "object",
                "properties": {
                    "temperature": {
                        "type": "number",
                        "description": "Temperature value (0.1-1.5, default 0.75)",
                    },
                },
                "required": ["temperature"],
            },
        ),
        Tool(
            name="voice_status",
            description="Check status of AllTalk TTS and Whisper STT services, and list available voices.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    global current_voice, current_speed, current_temperature, _services_available

    # voice_status always allowed (reports service state for diagnostics)
    if name != "voice_status" and not _services_available:
        # Re-check (services may have started since server launch)
        alltalk_ok = whisper_ok = False
        try:
            r = requests.get(f"{ALLTALK_URL}/api/ready", timeout=2)
            alltalk_ok = r.status_code == 200
        except Exception:
            pass
        try:
            r = requests.get(f"{WHISPER_URL}/health", timeout=2)
            whisper_ok = r.status_code == 200
        except Exception:
            pass

        if alltalk_ok and whisper_ok:
            _services_available = True
            logger.info("Services now available. Voice mode activated.")
        else:
            return [TextContent(type="text", text=json.dumps({
                "status": "unavailable",
                "message": "Voice services not running. Start them with start_claude_code_voice_mode.bat",
                "alltalk": "OK" if alltalk_ok else "offline",
                "whisper": "OK" if whisper_ok else "offline",
            }))]

    # If voice mode was disabled by recovery, block speak/listen/converse
    if name in ("speak", "listen", "converse") and _voice_mode_disabled:
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "voice_mode_disabled": True,
            "message": "Voice mode is disabled because the user could not hear audio. Communicate via text only. Offer to investigate the audio problem.",
        }))]

    if name == "speak":
        text = arguments.get("text", "")
        voice = arguments.get("voice")
        result = await asyncio.to_thread(speak_text, text, voice)
        return [TextContent(type="text", text=json.dumps(result))]

    elif name == "listen":
        duration = arguments.get("duration", 10.0)
        silence_timeout = arguments.get("silence_timeout", 2.0)
        audio = await asyncio.to_thread(record_audio, duration, silence_timeout)
        if len(audio) == 0:
            return [TextContent(type="text", text="No audio captured.")]
        text = await asyncio.to_thread(transcribe_audio, audio)
        return [TextContent(type="text", text=text if text else "Could not transcribe audio.")]

    elif name == "converse":
        message = arguments.get("message", "")
        listen_duration = arguments.get("listen_duration", 10.0)

        # Speak the message first
        if message:
            await asyncio.to_thread(speak_text, message)

        # Then listen for response
        audio = await asyncio.to_thread(record_audio, listen_duration, 2.0)
        if len(audio) == 0:
            return [TextContent(type="text", text="No response heard.")]
        text = await asyncio.to_thread(transcribe_audio, audio)
        return [TextContent(type="text", text=text if text else "Could not transcribe response.")]

    elif name == "set_voice":
        voice = arguments.get("voice", DEFAULT_VOICE)
        current_voice = voice
        logger.info(f"Voice changed to: {current_voice}")
        return [TextContent(type="text", text=f"Voice set to: {current_voice}")]

    elif name == "set_speed":
        speed = arguments.get("speed", 1.0)
        speed = max(0.5, min(2.0, float(speed)))
        current_speed = speed
        logger.info(f"Playback speed changed to: {current_speed}x")
        return [TextContent(type="text", text=f"Playback speed set to: {current_speed}x")]

    elif name == "set_temperature":
        temp = arguments.get("temperature", 0.75)
        temp = max(0.1, min(1.5, float(temp)))
        current_temperature = temp
        logger.info(f"Temperature changed to: {current_temperature}")
        return [TextContent(type="text", text=f"Temperature set to: {current_temperature}")]

    elif name == "voice_status":
        status = {"alltalk": "unknown", "whisper": "unknown", "voice": current_voice, "speed": get_playback_speed(), "temperature": current_temperature, "voices": [], "tts_paused": is_tts_paused(), "streaming_available": _streaming_available, "voice_mode_disabled": _voice_mode_disabled}
        try:
            r = requests.get(f"{ALLTALK_URL}/api/ready", timeout=3)
            status["alltalk"] = "ready" if r.status_code == 200 else f"error ({r.status_code})"
        except Exception as e:
            status["alltalk"] = f"offline ({e})"
        try:
            r = requests.get(f"{WHISPER_URL}/health", timeout=3)
            status["whisper"] = "ready" if r.status_code == 200 else f"error ({r.status_code})"
        except Exception as e:
            status["whisper"] = f"offline ({e})"
        try:
            r = requests.get(f"{ALLTALK_URL}/api/voices", timeout=3)
            if r.status_code == 200:
                status["voices"] = r.json() if isinstance(r.json(), list) else r.json().get("voices", [])
        except Exception:
            pass
        return [TextContent(type="text", text=json.dumps(status, indent=2))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    from mcp.server.stdio import stdio_server

    global _services_available

    logger.info("Claude Code Voice Mode MCP server starting...")
    logger.info(f"AllTalk TTS: {ALLTALK_URL}")
    logger.info(f"Whisper STT: {WHISPER_URL}")
    logger.info(f"Default voice: {DEFAULT_VOICE}")

    # Health check — are voice services actually running?
    alltalk_ok = False
    whisper_ok = False
    try:
        r = requests.get(f"{ALLTALK_URL}/api/ready", timeout=2)
        alltalk_ok = r.status_code == 200
    except Exception:
        pass
    try:
        r = requests.get(f"{WHISPER_URL}/health", timeout=2)
        whisper_ok = r.status_code == 200
    except Exception:
        pass

    _services_available = alltalk_ok and whisper_ok

    if _services_available:
        logger.info("Services detected: AllTalk OK, Whisper OK. Voice mode ACTIVE.")
    else:
        logger.warning(
            f"Services not ready (AllTalk={'OK' if alltalk_ok else 'OFFLINE'}, "
            f"Whisper={'OK' if whisper_ok else 'OFFLINE'}). "
            f"Voice tools will return 'not available'. Start services with start_claude_code_voice_mode.bat"
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
