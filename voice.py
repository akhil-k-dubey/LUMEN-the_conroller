"""
voice.py — Voice I/O pipeline for FRIDAY.

IN  → sounddevice → Silero VAD → faster-whisper (RTX 4050) → text
OUT → Kokoro ONNX (local neural TTS, ~50-150ms/sentence) → sounddevice

Why Kokoro instead of Edge TTS:
  - 100% local — no network, no rate limits, no "No audio received" failures
  - ~50-150ms generation after first call (model stays warm in memory)
  - No ffplay subprocess needed — audio played directly via sounddevice
  - Same kokoro-v1.0.onnx + voices-v1.0.bin you already downloaded
"""
from __future__ import annotations

import os
import queue
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Optional, Iterator

warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)

from ear import (
    np, sd, sr,
    setup_mic, setup_stt_backend,
    StreamingVoiceCapture, _try_import,
)

# ── Kokoro ONNX local TTS ─────────────────────────────────────────────────
_Kokoro = _try_import("kokoro_onnx", "Kokoro")

# Paths to local model files (already downloaded)
_KOKORO_MODEL  = os.path.join(os.path.dirname(__file__), "kokoro-v1.0.onnx")
_KOKORO_VOICES = os.path.join(os.path.dirname(__file__), "voices-v1.0.bin")

# Voice selection — Supports native en voices or beautiful custom blended recipes
# Predefined custom voice blends (perfect for a highly emotional, Irish/Friday voice)
_CUSTOM_VOICE_BLENDS = {
    "friday_irish_rose":      [("bf_emma", 0.50), ("af_nicole", 0.35), ("if_sara", 0.15)],
    "friday_celtic_whisper":  [("bf_isabella", 0.55), ("af_sarah", 0.30), ("ff_siwis", 0.15)],
    "friday_gaelic_aurora":   [("bf_lily", 0.50), ("af_nicole", 0.30), ("ef_dora", 0.20)],
    "friday_youth_spark":     [("af_sky", 0.50), ("af_bella", 0.35), ("bf_lily", 0.15)],
}

FRIDAY_VOICE = "friday_youth_spark"  # Youthful, energetic, conversational voice blend
FRIDAY_SPEED = 1.05                 # Slightly adjusted for natural lyrical flow
FRIDAY_LANG  = "en-gb"              # British English (closest phonetic matching)


@dataclass
class VoiceIO:
    """
    Full-duplex voice pipeline.
    IN  → StreamingVoiceCapture (Silero VAD + Whisper GPU)
    OUT → Kokoro ONNX (local neural TTS) → sounddevice
    """

    voice_mode:            bool  = True
    mic_enabled:           bool  = False
    tts_enabled:           bool  = True
    recognizer:            Optional[object] = None
    mic_backend:           str   = "none"
    stt_backend:           str   = "whisper"
    tts_backend:           str   = "auto"
    whisper_model_size:    str   = "small.en"
    stt_language:          str   = "en-IN"
    post_tts_cooldown_s:   float = 1.2
    debug:                 bool  = False
    whisper_model:         Optional[object] = None
    tts_effective_backend: str   = "none"

    _last_tts_time:          float           = field(default=0.0,                repr=False)
    _playback_stop:          threading.Event = field(default_factory=threading.Event, repr=False)
    _streaming_capture:      Optional[StreamingVoiceCapture] = field(default=None, repr=False)
    _kokoro:                 Optional[object] = field(default=None, repr=False)
    _kokoro_lock:            threading.Lock  = field(default_factory=threading.Lock, repr=False)
    _current_playback:       Optional[threading.Thread] = field(default=None, repr=False)
    _barge_in_speech_pending: bool           = field(default=False, repr=False)
    _alarm_queue:            queue.Queue     = field(default_factory=queue.Queue, repr=False)
    _current_voice_style:    Optional[object] = field(default=None, repr=False)
    played_sentences:        list[str]       = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # ── Mic setup ──
        self.recognizer, self.mic_enabled, self.mic_backend = setup_mic(
            self.recognizer, self._debug
        )
        self.stt_backend, self.whisper_model = setup_stt_backend(
            self.whisper_model_size, self._debug
        )

        # ── Kokoro TTS setup ──
        self._init_kokoro()

        # ── Streaming capture ──
        if self.mic_enabled and self.mic_backend == "sounddevice_stream":
            self._streaming_capture = StreamingVoiceCapture(
                stt_backend=self.stt_backend,
                whisper_model=self.whisper_model,
                recognizer=self.recognizer,
                stt_language=self.stt_language,
                debug=self._debug,
            )
            self._streaming_capture.start()
            self._debug("Streaming capture initialized.")

            # Wait for Silero VAD to be ready so we never miss the wake word
            self._debug("Waiting for Silero VAD...")
            ready = self._streaming_capture.vad.wait_ready(timeout=10)
            if ready:
                self._debug("Silero VAD ready.")
            else:
                self._debug("Silero VAD not ready (RMS fallback active).")

        # ── Alarm queue background thread ────────────────────────────────
        # Timer/reminder alerts are queued here so they don't block on speak()
        # when a pipeline is playing. A background daemon processes them.
        threading.Thread(target=self._alarm_worker, daemon=True, name="alarm-tts").start()

    def _resolve_voice_style(self, name: str):
        """Retrieve a voice style vector (embedding) or name. Supports custom blends."""
        if self._kokoro is None:
            return name
        if name in _CUSTOM_VOICE_BLENDS:
            self._debug(f"Building custom blended voice embedding: {name}")
            blend_recipe = _CUSTOM_VOICE_BLENDS[name]
            style = None
            for voice_key, weight in blend_recipe:
                v_style = self._kokoro.get_voice_style(voice_key)
                if style is None:
                    style = v_style * weight
                else:
                    style += v_style * weight
            return style
        return name

    def _init_kokoro(self) -> None:
        """Load Kokoro ONNX model from local files. Keeps model in memory."""
        if _Kokoro is None:
            self._debug("kokoro-onnx not installed — TTS disabled. Run: pip install kokoro-onnx")
            self.tts_enabled = False
            self.tts_effective_backend = "none"
            return

        if not os.path.exists(_KOKORO_MODEL):
            self._debug(f"Kokoro model not found: {_KOKORO_MODEL}")
            self.tts_enabled = False
            self.tts_effective_backend = "none"
            return

        if not os.path.exists(_KOKORO_VOICES):
            self._debug(f"Kokoro voices not found: {_KOKORO_VOICES}")
            self.tts_enabled = False
            self.tts_effective_backend = "none"
            return

        try:
            self._debug("Loading Kokoro ONNX TTS model...")
            t0 = time.monotonic()
            self._kokoro = _Kokoro(_KOKORO_MODEL, _KOKORO_VOICES)
            elapsed = time.monotonic() - t0
            self._debug(f"TTS: Kokoro ONNX ({FRIDAY_VOICE}) loaded in {elapsed:.2f}s [OK]")
            self.tts_enabled = True
            self.tts_effective_backend = "kokoro"

            # Pre-compute voice style
            self._current_voice_style = self._resolve_voice_style(FRIDAY_VOICE)

            # Warm-up call so the first real speak() is fast
            self._debug("Warming up Kokoro TTS...")
            self._kokoro.create("Ready.", voice=self._current_voice_style, speed=FRIDAY_SPEED, lang=FRIDAY_LANG)
            self._debug("Kokoro TTS warm-up complete.")

        except Exception as e:
            self._debug(f"Kokoro TTS failed to load: {e}")
            self._kokoro = None
            self.tts_enabled = False
            self.tts_effective_backend = "none"

    def _debug(self, msg: str) -> None:
        if self.debug:
            try:
                print(f"[voice-debug] {msg}")
            except UnicodeEncodeError:
                # Windows cp1252 console can't print Unicode arrows (→, ≥, etc.)
                # Replace unprintable chars with '?' to prevent daemon thread crashes.
                safe = msg.encode("ascii", errors="replace").decode("ascii")
                print(f"[voice-debug] {safe}")

    # ── Shutdown ──────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._streaming_capture:
            self._streaming_capture._running = False  # signal listen loop to exit NOW
            self._streaming_capture.stop()
            self._streaming_capture = None
        self._playback_stop.set()
        self._barge_in_speech_pending = False  # clean state on exit
        self.stop_playback()

    # ── Mic mute (for multi-message TTS blocks like boot) ─────────────

    def mute_mic(self) -> None:
        """Manually mute mic capture. Use for multi-speak blocks (boot, etc.).
        While muted, _play_audio() skips its own pause/resume.
        Call unmute_mic() when the block is done."""
        if self._streaming_capture:
            self._streaming_capture.pause_for_tts()

    def unmute_mic(self) -> None:
        """Unmute mic after a mute_mic() block. Flushes stale audio + settles."""
        if self._streaming_capture:
            self._streaming_capture.resume_after_tts()
        self._last_tts_time = time.monotonic()

    # ── Listen ────────────────────────────────────────────────────────────

    def listen(self) -> Optional[str]:
        """Block until user speaks; return transcript or None."""
        # Streaming path does not need sr at all — sr is only used by the
        # speech_recognition_mic fallback below. Checking sr is None here
        # would silently kill the streaming path if SpeechRecognition is
        # missing/broken, making Friday permanently deaf on sounddevice_stream.
        if not self.voice_mode or not self.mic_enabled:
            return None
        if time.monotonic() - self._last_tts_time < self.post_tts_cooldown_s:
            return None

        if self._streaming_capture:
            transcript = self._streaming_capture.listen_for_utterance()
            if transcript and transcript.strip():
                # Automatically enroll or update owner voiceprint if not enrolled yet!
                history_dir = os.path.dirname(__file__)
                vp_path = os.path.join(history_dir, "owner_voiceprint.npy")
                if not os.path.exists(vp_path) and (not hasattr(self, "_owner_voiceprint") or self._owner_voiceprint is None):
                    last_audio = getattr(self._streaming_capture, "_last_transcribed_audio", None)
                    if last_audio is not None:
                        self.enroll_owner_voice(last_audio)
            return transcript

        # Fallback: speech_recognition mic (PyAudio backend)
        if sr is None:
            return None
        if self.mic_backend == "speech_recognition_mic" and self.recognizer is not None:
            try:
                with sr.Microphone() as source:
                    self.recognizer.adjust_for_ambient_noise(source, duration=0.2)
                    audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=8)
                transcript = self.recognizer.recognize_google(audio, language=self.stt_language)
                return transcript.strip() if transcript else None
            except Exception as e:
                self._debug(f"STT mic fallback failed: {e}")
        return None

    # ── Core TTS generation ───────────────────────────────────────────────

    def _generate_tts(self, text: str, lock_timeout: float = 8.0) -> Optional[tuple]:
        """
        Generate audio via Kokoro ONNX — fully local, ~50-150ms per sentence.
        Returns (samples_float32, sample_rate) or None on failure.
        No network, no rate limits, no temp files.

        ``lock_timeout`` caps how long we wait for the Kokoro lock.
        The pipeline's tts-worker holds the lock while generating sentences;
        a background alarm thread calling this would block indefinitely with
        an unconditional ``with`` block.  A bounded timeout lets the alarm
        degrade to print-only instead of hanging.
        """
        if self._kokoro is None or not text.strip():
            return None

        # Strip characters that Kokoro doesn't handle well
        clean = text.replace("[", "").replace("]", "").replace("*", "").strip()
        if not clean:
            return None

        acquired = self._kokoro_lock.acquire(timeout=lock_timeout)
        if not acquired:
            self._debug(
                f"TTS Kokoro lock timeout ({lock_timeout:.0f}s) — "
                f"skipping: {clean[:50]!r}"
            )
            return None

        try:
            t0 = time.monotonic()
            samples, sample_rate = self._kokoro.create(
                clean,
                voice=self._current_voice_style,
                speed=FRIDAY_SPEED,
                lang=FRIDAY_LANG,
            )
            elapsed = (time.monotonic() - t0) * 1000
            self._debug(f"TTS Kokoro ({elapsed:.0f}ms): {clean[:50]!r}")
            return (samples, sample_rate)
        except Exception as e:
            self._debug(f"TTS Kokoro failed: {e}")
            return None
        finally:
            self._kokoro_lock.release()

    # ── Playback ──────────────────────────────────────────────────────────

    def _play_audio(self, samples, sample_rate: int) -> None:
        """Play float32 audio array via sounddevice. Blocks until done."""
        if sd is None or samples is None:
            return
        # If mic is already muted (e.g., boot block or speak_pipeline),
        # skip our own pause/resume to avoid double-unmuting mid-sequence.
        already_muted = (
            self._streaming_capture is not None
            and self._streaming_capture._speaking
        )
        if self._streaming_capture and not already_muted:
            self._streaming_capture.pause_for_tts()
        _out_stream = None
        try:
            import numpy as _np
            _out_stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=1 if samples.ndim == 1 else samples.shape[1],
                dtype='float32',
            )
            _out_stream.start()
            chunk_size = sample_rate // 10  # 100ms chunks
            flat = samples.flatten() if samples.ndim > 1 else samples
            offset = 0
            play_duration = len(flat) / sample_rate
            deadline = time.monotonic() + play_duration + 2.0
            while offset < len(flat):
                if self._playback_stop.is_set():
                    break
                if time.monotonic() > deadline:
                    self._debug("[play_audio] playback timeout — force stopping output")
                    break
                end = min(offset + chunk_size, len(flat))
                _out_stream.write(flat[offset:end].reshape(-1, 1) if _out_stream.channels == 1 else flat[offset:end])
                offset = end
        except KeyboardInterrupt:
            self._debug("[play_audio] interrupted by user")
            self._playback_stop.set()
        except Exception as e:
            self._debug(f"Audio playback failed: {e}")
        finally:
            if _out_stream is not None:
                try:
                    _out_stream.stop()
                    _out_stream.close()
                except Exception:
                    pass
            if self._streaming_capture and not already_muted:
                self._streaming_capture.resume_after_tts()

    # ── TTS Cache helpers ──────────────────────────────────────────────────

    def _read_wav_cache(self, path: str) -> Optional[tuple[np.ndarray, int]]:
        import wave
        import numpy as np
        try:
            with wave.open(path, "rb") as wav:
                n_channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                n_frames = wav.getnframes()
                frames = wav.readframes(n_frames)
                
                if sample_width == 2:
                    data = np.frombuffer(frames, dtype=np.int16)
                    samples = data.astype(np.float32) / 32767.0
                    return samples, sample_rate
        except Exception as e:
            self._debug(f"[tts-cache] Failed to read wav cache: {e}")
        return None

    def _write_wav_cache(self, path: str, samples: np.ndarray, sample_rate: int) -> None:
        import wave
        import numpy as np
        try:
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                int16_data = (samples * 32767).clip(-32768, 32767).astype(np.int16)
                wav.writeframes(int16_data.tobytes())
        except Exception as e:
            self._debug(f"[tts-cache] Failed to write wav cache: {e}")

    def _get_or_render_static(self, text: str, lock_timeout: float = 8.0) -> Optional[tuple[np.ndarray, int]]:
        """Retrieve pre-rendered TTS WAV from disk or generate and cache it."""
        import hashlib
        import os
        
        clean_text = self._clean_for_tts(text)
        if not clean_text:
            return None
            
        cache_dir = r"C:\Users\akhil\._cache_antigravity_tts"
        # Wait, let's use a robust cache directory inside .gemini directory
        cache_dir = r"C:\Users\akhil\.gemini\antigravity\tts_cache"
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            # Fallback to local workspace if permission issues
            cache_dir = os.path.join(os.path.dirname(__file__), ".tts_cache")
            os.makedirs(cache_dir, exist_ok=True)
            
        # Hash voice CONFIG, not the numpy array (which varies in repr between runs)
        key = f"{clean_text}_{FRIDAY_VOICE}_{FRIDAY_SPEED}_{FRIDAY_LANG}"
        h = hashlib.sha256(key.encode('utf-8')).hexdigest()
        cache_path = os.path.join(cache_dir, f"{h}.wav")
        
        if os.path.exists(cache_path):
            cached = self._read_wav_cache(cache_path)
            if cached:
                self._debug(f"[tts-cache] HIT: loaded pre-rendered WAV for: {clean_text[:40]!r}")
                return cached
                
        # Cache MISS
        result = self._generate_tts(clean_text, lock_timeout=lock_timeout)
        if result:
            samples, sr_rate = result
            self._write_wav_cache(cache_path, samples, sr_rate)
            self._debug(f"[tts-cache] MISS: pre-rendered and saved to: {cache_path}")
            return samples, sr_rate
            
        return None

    # ── speak() — single-shot ─────────────────────────────────────────────

    def speak(self, text: str) -> None:
        """Generate and play TTS synchronously (boot messages, short replies)."""
        text = self._clean_for_tts(text)
        if not self.voice_mode or not text.strip() or not self.tts_enabled:
            return
        self._playback_stop.clear()  # reset barge-in flag from prior pipeline
        result = self._get_or_render_static(text)
        if result:
            samples, sr_rate = result
            try:
                self._play_audio(samples, sr_rate)
                self._last_tts_time = time.monotonic()
                self._debug(f"Spoke: {text[:60]}")
            except Exception as e:
                self._debug(f"speak() playback failed: {e}")
        else:
            self._debug(f"speak() TTS failed for: {text[:40]!r}")

    # ── Alarm/alert queue (non-blocking TTS for timer/reminder) ───────────

    def _alarm_worker(self) -> None:
        """Background daemon: process alert texts from the alarm queue."""
        while True:
            text = self._alarm_queue.get()
            if text is None:
                break
            text = self._clean_for_tts(text)
            if not text.strip():
                continue
            try:
                result = self._get_or_render_static(text, lock_timeout=3.0)
                if result:
                    samples, sr_rate = result
                    import numpy as _np

                    # Wait if the assistant is currently speaking to avoid overlapping output
                    while self._streaming_capture and self._streaming_capture._speaking:
                        time.sleep(0.2)

                    out = sd.OutputStream(
                        samplerate=sr_rate, channels=1, dtype='float32'
                    )
                    out.start()
                    flat = samples.flatten() if samples.ndim > 1 else samples
                    chunk_size = sr_rate // 10
                    offset = 0
                    while offset < len(flat):
                        end = min(offset + chunk_size, len(flat))
                        out.write(flat[offset:end].reshape(-1, 1))
                        offset = end
                    out.stop()
                    out.close()
                    self._last_tts_time = time.monotonic()
            except Exception:
                pass

    def speak_alarm(self, text: str) -> None:
        """Queue a high-priority alert for immediate TTS (non-blocking)."""
        if not self.voice_mode or not text.strip() or not self.tts_enabled:
            return
        self._alarm_queue.put(text)

    def _extract_voiceprint(self, samples_16k: np.ndarray) -> Optional[np.ndarray]:
        """Extract a normalized 64-dimensional Mel spectral envelope from 16kHz audio."""
        try:
            import torch
            import torchaudio
            
            # Convert NumPy array to Torch tensor
            tensor = torch.tensor(samples_16k, dtype=torch.float32)
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0) # (1, N)
                
            # Extract Mel Spectrogram
            mel_transform = torchaudio.transforms.MelSpectrogram(
                sample_rate=16000,
                n_fft=400,
                win_length=400,
                hop_length=160,
                n_mels=64
            )
            
            with torch.no_grad():
                mel_spec = mel_transform(tensor) # (1, n_mels, time)
                log_mel = torch.log(mel_spec + 1e-6)
                envelope = log_mel.mean(dim=2).squeeze(0) # (n_mels,)
                envelope = envelope / (torch.norm(envelope) + 1e-8)
                
            return envelope.numpy()
        except Exception as e:
            self._debug(f"[speaker-id] Voiceprint extraction failed: {e}")
            return None

    def _verify_speaker(self, speech_chunks: list) -> bool:
        """
        Verify if the captured speech chunks match the enrolled owner's voiceprint.
        Returns True if matched or if no voiceprint is enrolled yet.
        """
        if not speech_chunks:
            return False
            
        # Concatenate speech chunks (already decimated to 16kHz)
        audio = np.concatenate(speech_chunks)
        
        # We need at least ~100ms of audio
        if len(audio) < 1600:
            return False
            
        # Extract voiceprint
        vp = self._extract_voiceprint(audio)
        if vp is None:
            return True # fallback: allow if extraction fails
            
        # Load enrolled voiceprint if not in memory
        history_dir = os.path.dirname(__file__)
        vp_path = os.path.join(history_dir, "owner_voiceprint.npy")
        
        # Load from disk if needed
        if not hasattr(self, "_owner_voiceprint") or self._owner_voiceprint is None:
            if os.path.exists(vp_path):
                try:
                    self._owner_voiceprint = np.load(vp_path)
                    self._debug("[speaker-id] Loaded enrolled owner voiceprint from disk.")
                except Exception as e:
                    self._debug(f"[speaker-id] Failed to load voiceprint: {e}")
                    self._owner_voiceprint = None
            else:
                self._owner_voiceprint = None
                
        # If no voiceprint is enrolled yet, allow the barge-in
        if self._owner_voiceprint is None:
            self._debug("[speaker-id] No owner voiceprint enrolled. Allowing barge-in.")
            return True
            
        # Compute cosine similarity
        v1 = self._owner_voiceprint
        v2 = vp
        similarity = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8)
        
        # Dynamic threshold matching:
        # A conservative threshold for speaker verification
        THRESHOLD = 0.93
        matched = similarity >= THRESHOLD
        
        self._debug(f"[speaker-id] Cosine Similarity: {similarity:.4f} (match={matched})")
        return matched

    def enroll_owner_voice(self, samples_16k: np.ndarray) -> None:
        """Enroll the owner's voiceprint from a successful speech input."""
        if len(samples_16k) < 8000: # at least 0.5s of audio
            return
            
        vp = self._extract_voiceprint(samples_16k)
        if vp is not None:
            self._owner_voiceprint = vp
            history_dir = os.path.dirname(__file__)
            vp_path = os.path.join(history_dir, "owner_voiceprint.npy")
            try:
                np.save(vp_path, vp)
                self._debug(f"[speaker-id] Successfully enrolled owner voiceprint to {vp_path}.")
            except Exception as e:
                self._debug(f"[speaker-id] Failed to save enrolled voiceprint: {e}")

    # ── Barge-in monitor ─────────────────────────────────────────────────

    def _barge_in_monitor(self, stop_event: threading.Event, playback_started: threading.Event) -> None:
        """
        Daemon thread: watches the mic via Silero VAD while speak_pipeline() runs.

        Trigger rule: 2 consecutive 32ms chunks satisfying BOTH:
          (a) speech_prob ≥ BARGE_IN_THRESHOLD (0.80)
          (b) chunk RMS ≥ loopback_baseline_ema × RMS_GATE_MULTIPLIER (2.2×)
        → fires stop_playback() to cut TTS immediately.

        After confirmation, drains barge_q for ~300ms to capture the user's
        full interrupted sentence before handing off to listen_for_utterance().

        WHY DUAL GATE:
        The stream stays live during TTS (single-stream WASAPI design), so the
        mic picks up TTS loopback from speakers. Silero scores loopback as
        speech (correctly — it IS speech phonemes). Two discriminators separate
        loopback from real barge-in:

          1. RMS relative gate: loopback through room air is attenuated vs the
             user's voice directly into the mic. The EMA baseline tracks the
             loopback energy level; real speech must be louder (× RMS_GATE_MULTIPLIER).

          2. Duration (CONFIRM_CHUNKS=2 = 64ms): fast trigger for responsive
             interruption. 2 grace misses tolerate natural energy dips.

        Tune RMS_GATE_MULTIPLIER lower (2.0) if barge-in is hard to trigger,
        higher (3.5) if speaker loopback still causes false positives.
        """
        if np is None or self._streaming_capture is None:
            return

        BARGE_IN_THRESHOLD  = 0.80   # lowered: more sensitive to human voice
        CONFIRM_CHUNKS      = 2      # 2×32ms=64ms — fast trigger, still deliberate
        CHUNK_SIZE          = 512    # 32ms at 16kHz — Silero requirement
        RMS_GATE_MULTIPLIER = 2.2    # slightly easier for real user voice to beat (tuned from 3.0)
        RMS_EMA_ALPHA       = 0.12   # EMA smoothing for loopback baseline
        RMS_FLOOR           = 0.0015 # lower floor — allows low-gain/quiet mics to barge in (tuned from 0.008)
        MAX_BASELINE        = 0.020  # cap: need≥ never exceeds 0.020×2.2=0.044 (tuned from 0.025)
        #   Without this cap the EMA tracks loud laptop loopback (0.06–0.09 RMS)
        #   and pushes need≥ to 0.17+, which real user voice cannot beat.
        #   With the cap: loopback mostly rejected; user speaking clearly at
        #   0.05+ RMS for 160ms triggers reliably.

        vad              = self._streaming_capture.vad
        consecutive      = 0
        rms_baseline_ema = RMS_FLOOR  # start AT floor — never below the minimum gate
        _barge_in_confirmed = False
        # Adaptive confirm target — updated per chunk based on RMS confidence.
        _confirm_target  = CONFIRM_CHUNKS
        # Grace-miss counter: allows 1 soft-miss per barge-in run.
        # Human speech has natural inter-phoneme energy dips; a single weak
        # 32ms chunk should NOT destroy a confirmed run.
        _grace_remaining = 2   # allow 2 natural energy dips (was 1)
        # Speech chunks that passed BOTH gates — saved to pre-buffer on confirm.
        # Reset whenever consecutive drops to 0 (loopback broke the run).
        speech_chunks: list = []
        # RAW (pre-decimate) versions of speech_chunks — saved so that
        # listen_for_utterance() can run them through RNNoise to strip
        # loopback and recover the user's speech onset (~200ms).
        speech_chunks_raw: list = []

        # Wire up the queue — callback will feed it while _speaking=True
        #
        # Unbounded (maxsize=0): the monitor processes each chunk in ~1-3ms
        # (Silero CPU inference); chunks arrive every 32ms (31/sec at 48kHz).
        # The monitor runs at 10-30× the input rate — no backlog can build.
        # A bounded queue with put_nowait() would silently drop NEW chunks
        # while keeping STALE ones — exactly wrong for real-time detection.
        # The queue is GC'd when the pipeline ends, so unbounded is safe.
        barge_q: queue.Queue = queue.Queue()
        self._streaming_capture._barge_in_queue = barge_q

        # Wait for actual audio before watching for interrupts
        # If pipeline ends before audio ever plays (LLM failed etc), exit cleanly
        playback_started.wait(timeout=15.0)
        if stop_event.is_set():
            self._streaming_capture._barge_in_queue = None
            return

        # Collect baseline samples first to warm up the EMA
        WARMUP_CHUNKS = 8   # ~256ms of loopback to seed the EMA
        warmed = 0
        warmup_rms_values = []
        while warmed < WARMUP_CHUNKS and not stop_event.is_set():
            try:
                raw = barge_q.get(timeout=0.1)
            except queue.Empty:
                continue
            
            if self._streaming_capture._denoiser is not None:
                chunk = raw[::self._streaming_capture._DECIMATE]
            else:
                chunk = raw

            if len(chunk) < CHUNK_SIZE:
                chunk = np.pad(chunk, (0, CHUNK_SIZE - len(chunk)))
            elif len(chunk) > CHUNK_SIZE:
                chunk = chunk[:CHUNK_SIZE]

            rms = float(np.sqrt(np.mean(chunk ** 2)))
            if rms > 0.001:   # only update from non-silent chunks (real loopback)
                warmup_rms_values.append(rms)
                rms_baseline_ema = (
                    RMS_EMA_ALPHA * rms + (1 - RMS_EMA_ALPHA) * rms_baseline_ema
                )
            warmed += 1

        max_loopback = np.max(warmup_rms_values) if warmup_rms_values else 0.020
        if max_loopback > 0.035:
            dynamic_multiplier = 1.35
            dynamic_max_baseline = max_loopback * 1.05
        else:
            dynamic_multiplier = RMS_GATE_MULTIPLIER
            dynamic_max_baseline = 0.020

        self._debug(
            f"[barge-in] baseline warmed: {rms_baseline_ema:.4f}, "
            f"max loopback: {max_loopback:.4f}, dynamic mult: {dynamic_multiplier:.2f}, "
            f"dynamic cap: {dynamic_max_baseline:.4f}"
        )

        import collections
        rolling_prebuf = collections.deque(maxlen=25)  # ~800ms of onset capture

        try:
            while not stop_event.is_set():
                try:
                    raw = barge_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if raw is None:
                    break  # sentinel from pipeline end — exit immediately

                # ── Decimate to 16kHz (inline, no shared DSP state) ─────────
                # We intentionally skip _process_chunk() / RNNoise here.
                # _denoiser.denoise_chunk() mutates a stateful GRU — calling it
                # from this thread concurrently with listen_for_utterance() would
                # be a data race on the RNNoise C context (corruption / crash).
                # Barge-in only needs a rough speech probability, not clean audio;
                # a plain decimate slice is sufficient and touches zero shared state.
                if self._streaming_capture._denoiser is not None:
                    chunk = raw[::self._streaming_capture._DECIMATE]  # 48kHz → 16kHz
                else:
                    chunk = raw  # stream is already 16kHz

                # Pad / trim to exactly 512 samples for Silero
                if len(chunk) < CHUNK_SIZE:
                    chunk = np.pad(chunk, (0, CHUNK_SIZE - len(chunk)))
                elif len(chunk) > CHUNK_SIZE:
                    chunk = chunk[:CHUNK_SIZE]

                rolling_prebuf.append(chunk.copy())

                prob = vad.speech_prob(chunk)
                rms  = float(np.sqrt(np.mean(chunk ** 2)))
                rms_required = max(
                    max_loopback * 1.3,  # must beat measured loopback by 30%
                    max(RMS_FLOOR, min(rms_baseline_ema, dynamic_max_baseline)) * dynamic_multiplier
                )

                if prob >= BARGE_IN_THRESHOLD:
                    # ── RMS relative gate ──────────────────────────────────
                    rms_ok = rms >= rms_required
                    if rms_ok:
                        # ── Adaptive confirmation window ───────────────────
                        ratio = rms / max(rms_required, 1e-9)
                        if ratio >= 2.0:
                            _confirm_target = max(3, CONFIRM_CHUNKS - 1)
                        elif ratio >= 1.3:
                            _confirm_target = CONFIRM_CHUNKS
                        else:
                            _confirm_target = CONFIRM_CHUNKS + 1
                        speech_chunks.append(chunk)       # save for pre-buffer
                        speech_chunks_raw.append(raw.copy())  # raw for RNNoise recovery
                        consecutive += 1
                        self._debug(
                            f"[barge-in] prob={prob:.2f} rms={rms:.4f} "
                            f"need≥{rms_required:.4f} HIT {consecutive}/{_confirm_target}"
                        )
                        if consecutive >= _confirm_target:
                            if stop_event.is_set():
                                return
                            # NOTE: Speaker verification (voiceprint) is intentionally
                            # SKIPPED during barge-in. The mic audio contains TTS
                            # loopback mixed with the user's voice, so the mel
                            # spectrogram never matches the clean enrolled voiceprint.
                            # The RMS + VAD dual gate is sufficient to separate
                            # loopback from real speech.

                            self._debug("[barge-in] CONFIRMED — saving audio + stopping playback")
                            _barge_in_confirmed = True
                            
                            # ── Drain barge_q for ~300ms to capture the user's full sentence ──
                            # After confirmation, the user is still talking. We need to
                            # grab those chunks before stop_playback() kills the route.
                            drain_deadline = time.monotonic() + 0.30
                            drain_chunks = []
                            while time.monotonic() < drain_deadline:
                                try:
                                    drain_raw = barge_q.get(timeout=0.04)
                                    if drain_raw is None:
                                        break
                                    if self._streaming_capture._denoiser is not None:
                                        drain_chunk = drain_raw[::self._streaming_capture._DECIMATE]
                                    else:
                                        drain_chunk = drain_raw
                                    if len(drain_chunk) < CHUNK_SIZE:
                                        drain_chunk = np.pad(drain_chunk, (0, CHUNK_SIZE - len(drain_chunk)))
                                    elif len(drain_chunk) > CHUNK_SIZE:
                                        drain_chunk = drain_chunk[:CHUNK_SIZE]
                                    drain_chunks.append(drain_chunk.copy())
                                except queue.Empty:
                                    continue

                            # Save ALL rolling pre-buffer + drain chunks (~800ms + 300ms of onset)
                            all_processed = list(rolling_prebuf) + drain_chunks
                            with self._streaming_capture._barge_in_lock:
                                self._streaming_capture._barge_in_audio_buffer = all_processed
                            self._debug(
                                f"[barge-in] pre-buffer: {len(all_processed)} chunks "
                                f"({len(all_processed)*32}ms) saved"
                            )
                            self.stop_playback()
                            return
                    else:
                        # ── Soft-miss tolerance ──────────────────────────────
                        # Human speech has natural inter-phoneme energy dips.
                        # If we are mid-run (consecutive ≥ 1) AND this chunk is
                        # close to the threshold (≥ 70% of required RMS), AND
                        # we have grace remaining, apply a soft reset:
                        # decrement by 1 instead of wiping to 0.
                        # This tolerates one genuinely weak 32ms phoneme in the
                        # middle of real user speech.
                        close_enough = rms >= rms_required * 0.70
                        if consecutive >= 1 and close_enough and _grace_remaining > 0:
                            _grace_remaining -= 1
                            consecutive = max(0, consecutive - 1)
                            self._debug(
                                f"[barge-in] prob={prob:.2f} rms={rms:.4f} "
                                f"need≥{rms_required:.4f} SOFT-MISS ({consecutive} left)"
                            )
                            # Do NOT update EMA — this chunk is probably user voice
                        else:
                            consecutive = 0
                            speech_chunks = []       # reset — this run was loopback
                            speech_chunks_raw = []
                            _grace_remaining = 2     # restore grace for next run
                            self._debug(
                                f"[barge-in] prob={prob:.2f} rms={rms:.4f} "
                                f"need≥{rms_required:.4f} loopback"
                            )
                            # Update baseline from high-prob loopback
                            rms_baseline_ema = min(
                                MAX_BASELINE,
                                RMS_EMA_ALPHA * rms + (1 - RMS_EMA_ALPHA) * rms_baseline_ema
                            )
                elif prob >= 0.50:
                    # Medium-probability chunk: Silero is uncertain — could be
                    # attenuated loopback or a soft consonant. Apply soft-miss
                    # if we have an active run, otherwise full reset.
                    if consecutive >= 1 and _grace_remaining > 0:
                        _grace_remaining -= 1
                        consecutive = max(0, consecutive - 1)
                        self._debug(
                            f"[barge-in] prob={prob:.2f} rms={rms:.4f} "
                            f"need≥{rms_required:.4f} SOFT-MISS/medium ({consecutive} left)"
                        )
                    else:
                        consecutive = 0
                        speech_chunks = []
                        speech_chunks_raw = []
                        _grace_remaining = 2
                        if rms < rms_required:
                            rms_baseline_ema = min(
                                MAX_BASELINE,
                                RMS_EMA_ALPHA * 0.5 * rms + (1 - RMS_EMA_ALPHA * 0.5) * rms_baseline_ema
                            )
                else:
                    consecutive = 0
                    speech_chunks = []
                    speech_chunks_raw = []
                    _grace_remaining = 2
                    # Quiet/noise chunk: do NOT update baseline.
        finally:
            # Only zero the cooldown on a NATURAL pipeline end where the user
            # was mid-word (consecutive>=1 but not yet confirmed). On a confirmed
            # barge-in, stop_playback() already zeroed _last_tts_time.
            if not _barge_in_confirmed and consecutive >= 1:
                self._last_tts_time = 0.0
                self._debug("[barge-in] partial speech at pipeline end — cooldown cleared")
            # Deregister queue — callback reverts to dropping audio when _speaking=True
            self._streaming_capture._barge_in_queue = None

    def _clean_for_tts(self, text: str) -> str:
        """Strip debug and interruption markers before generating TTS."""
        if not text:
            return ""
        import re
        text = re.sub(r'\s*\.\.\.\s*\[interrupted\]', '', text)
        text = re.sub(r'\s*\[interrupted\]', '', text)
        text = text.replace('...', '')
        
        # Filter standalone numbers or list indices (e.g. "2." or "2")
        if re.match(r"^\s*\d+\.?\s*$", text):
            return ""
            
        return text.strip()

    # ── speak_pipeline() — 3-thread parallel pipeline ────────────────────

    def speak_pipeline(self, sentences: Iterator[str]) -> None:
        """
        TRUE 3-thread parallel pipeline:

          Thread 1 (llm-feeder): pulls text from LLM generator → sentence_q
          Thread 2 (tts-worker): sentence_q → Kokoro generate → audio_q
          Main thread (player):  audio_q → sounddevice.play()

        While sentence N is PLAYING, sentence N+1 is being TTS-generated.
        Kokoro is ~50-150ms vs Edge TTS ~1500ms → massively lower latency.

        Filters [ACTION:...] tags — handled by skill dispatch.

        Mic lifecycle (pipeline-level, not per-sentence):
          pause_for_tts()  ← called ONCE here, before any thread starts
              guarantees _speaking=True before barge_q is registered,
              so the callback feeds the monitor from the very first chunk.
              _play_audio() sees already_muted=True and skips its own
              pause/resume — no per-sentence churn.
          resume_after_tts() ← called ONCE after pipeline exits (normal or
              barge-in) — single clean handoff to listen_for_utterance().
        """
        if not self.voice_mode or not self.tts_enabled:
            return

        self._playback_stop.clear()
        self.played_sentences = []
        sentence_q: queue.Queue = queue.Queue()
        audio_q:    queue.Queue = queue.Queue()

        self._sentence_q = sentence_q
        self._audio_q = audio_q

        # ── Mute mic NOW — before the monitor thread even starts ──────────
        # This sets _speaking=True so that as soon as the monitor registers
        # barge_q, the callback immediately routes audio there.
        # No race window: monitor always has a live feed from chunk 1.
        if self._streaming_capture:
            self._streaming_capture.pause_for_tts()

        # ── Barge-in monitor — starts immediately, dies when pipeline ends ──
        _barge_in_stop = threading.Event()
        _playback_started = threading.Event()
        threading.Thread(
            target=self._barge_in_monitor,
            args=(_barge_in_stop, _playback_started),
            daemon=True,
            name="barge-in-monitor",
        ).start()

        # ── Thread 1: drain the LLM sentence generator ──
        def _sentence_feeder():
            for s in sentences:
                if self._playback_stop.is_set():
                    break
                s = self._clean_for_tts(s)
                if s and "[action:" not in s.lower():
                    sentence_q.put(s)
            sentence_q.put(None)  # sentinel

        threading.Thread(target=_sentence_feeder, daemon=True, name="llm-feeder").start()

        # ── Thread 2: TTS generation via Kokoro (~50-150ms each) ──
        def _tts_worker():
            try:
                while True:
                    try:
                        text = sentence_q.get(timeout=60.0)
                    except queue.Empty:
                        break
                    if text is None:
                        break  # text sentinel from feeder
                    if self._playback_stop.is_set():
                        break  # barge-in fired — exit without generating more TTS
                    result = self._get_or_render_static(text)
                    if self._playback_stop.is_set():
                        break
                    if result:
                        audio_q.put((result[0], result[1], text))
                    else:
                        pass  # skip failed TTS runs without breaking player sentinel
            finally:
                audio_q.put(None)  # sentinel — exactly once, on every exit path

        threading.Thread(target=_tts_worker, daemon=True, name="tts-worker").start()

        # ── Main thread: playback ──
        # _play_audio() sees already_muted=True (we set _speaking above) and
        # skips its own pause_for_tts / resume_after_tts on every sentence.
        #
        # CRITICAL: entire block in try/finally so resume_after_tts() ALWAYS
        # runs — a WASAPI crash must not leave the pipeline stuck in SPEAKING.
        try:
            while True:
                try:
                    result = audio_q.get(timeout=60.0)
                except queue.Empty:
                    break
                if result is None:
                    break  # sentinel
                if self._playback_stop.is_set():
                    break
                samples, sample_rate, text = result
                try:
                    _playback_started.set()
                    self._play_audio(samples, sample_rate)
                    self._last_tts_time = time.monotonic()
                    if not self._playback_stop.is_set():
                        self.played_sentences.append(text)
                        self._debug(f"Spoke: {text[:60]}")
                except Exception as e:
                    self._debug(f"Pipeline play failed: {e}")
        except Exception as e:
            self._debug(f"Pipeline loop crashed: {e}")
        finally:
            # ── Single clean handoff — normal finish OR barge-in ─────────
            # ALWAYS runs: stop monitor, restore mic, clear queues.
            _barge_in_stop.set()
            try:
                barge_q = self._streaming_capture._barge_in_queue if self._streaming_capture else None
                if barge_q is not None:
                    barge_q.put_nowait(None)
            except Exception:
                pass
            if self._streaming_capture:
                was_barge_in = self._streaming_capture._barge_in_fired
                self._streaming_capture.resume_after_tts(barge_in=was_barge_in)
            self._sentence_q = None
            self._audio_q = None

    def stop_playback(self) -> None:
        """Barge-in: stop sounddevice playback immediately.

        Sets _playback_stop so the _play_audio write-loop exits cleanly.
        Does NOT call sd.stop() — that would kill the mic InputStream and
        cause listen_for_utterance() to hang forever after barge-in.

        CRITICAL: Sets _speaking=False so the audio callback immediately
        routes mic data to _audio_queue. Without this, audio is DROPPED
        between barge-in confirmation and resume_after_tts() (~100-300ms),
        causing Whisper to miss the user's first words.
        """
        self._playback_stop.set()
        # Zero the cooldown so listen() doesn't wait 1.2s after barge-in.
        self._last_tts_time = 0.0
        self._barge_in_speech_pending = True

        # Atomically flush queues to prevent any remaining sentences from generating or playing
        if hasattr(self, "_sentence_q") and self._sentence_q is not None:
            while not self._sentence_q.empty():
                try:
                    self._sentence_q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._sentence_q.put_nowait(None)
            except Exception:
                pass
        if hasattr(self, "_audio_q") and self._audio_q is not None:
            while not self._audio_q.empty():
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._audio_q.put_nowait(None)
            except Exception:
                pass

        # Signal the capture so resume_after_tts() skips its 300ms sleep.
        # The user is already speaking — any delay clips their first word.
        if self._streaming_capture is not None:
            self._streaming_capture._barge_in_fired = True
            # Route mic audio to _audio_queue IMMEDIATELY.
            # Without this, _speaking=True + _barge_in_queue=None (deregistered
            # by monitor's finally block) = audio silently dropped for ~200ms.
            self._streaming_capture._speaking = False