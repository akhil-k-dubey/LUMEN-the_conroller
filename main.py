"""
main.py — FRIDAY Advanced AI Assistant.

Architecture:
  1. LLM streams tokens → accumulates sentences
  2. First sentence pre-gen starts immediately (latency hiding)
  3. TTS plays sentence-by-sentence with minimal gaps
  4. Skill actions are detected, executed, and results spoken
  5. Persistent memory logs every interaction

All tools are FREE FOREVER — no API keys required.
"""
from __future__ import annotations

import argparse
import re
import queue
import signal
import sys
import threading
import warnings
from datetime import datetime
from typing import Optional, Any
import builtins

_original_print = builtins.print
_print_lock = threading.Lock()

def _thread_safe_print(*args, **kwargs):
    with _print_lock:
        _original_print(*args, **kwargs)

builtins.print = _thread_safe_print

# Silence pkg_resources deprecation from webrtcvad
warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)

from assistant import Friday
from brain import Brain
from commands import is_shutdown_command, is_wake_command
from memory import PersistentMemory
from monitor import ProactiveMonitor
from skills import SkillEngine
from voice import VoiceIO
from agentic import AgenticTaskLoop, is_agentic_task
from skills import detect_direct_intent


# ── Speculative LLM Prefill State ──────────────────────────────────────────
power_saving_mode: bool = False
speculative_llm_lock = threading.Lock()
_spec_llm_thread: Optional[threading.Thread] = None
_spec_llm_transcript: Optional[str] = None
_spec_llm_generator: Optional[Any] = None
_spec_llm_active: bool = False
_spec_llm_done: bool = False
_spec_llm_abort = threading.Event()
_spec_llm_sentences: queue.Queue = queue.Queue()
_spec_llm_history_snapshot: int = 0     # len(conversation_history) before speculative run
_spec_llm_brain_ref: Optional[Any] = None  # back-ref to Brain for rollback


def _are_semantically_similar(s1: str, s2: str) -> bool:
    import re
    def _clean(t):
        t = t.lower()
        t = re.sub(r'[^\w\s]', '', t)
        contractions = {"whats": "what is", "im": "i am", "dont": "do not", "cant": "cannot", "youre": "you are", "lets": "let us"}
        for k, v in contractions.items():
            t = t.replace(k, v)
        num_map = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
        for k, v in num_map.items():
            t = t.replace(k, v)
        return t.strip().split()

    w1 = _clean(s1)
    w2 = _clean(s2)
    if not w1 or not w2:
        return False
    if w1 == w2:
        return True
    set1, set2 = set(w1), set(w2)
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))
    similarity = intersection / union if union > 0 else 0.0
    return similarity >= 0.80


REQUIRED_PARAMS = [
    {
        "intent": "calculate",
        "patterns": [r"\b(calculate|multiply|add|divide|subtract|math|compute|solve)\b"],
        "validator": lambda q: any(c.isdigit() for c in q),
        "prompt": "What numbers or expression would you like me to calculate?",
    },
    {
        "intent": "web_search",
        "patterns": [r"\bsearch the web\b", r"\bweb search\b", r"\bsearch online\b", r"\bgoogle search\b", r"\bsearch for\b"],
        "validator": lambda q: len(re.sub(r"\b(search the web|web search|search online|google search|search for|about|google|for|on)\b", "", q, flags=re.IGNORECASE).strip()) > 0,
        "prompt": "What would you like me to search for on the web?",
    },
    {
        "intent": "create_file",
        "patterns": [r"\b(create|write|make)\s+(a\s+)?file\b"],
        "validator": lambda q: any(ext in q.lower() for ext in [".txt", ".py", ".md", ".json", ".js", ".html", "named"]),
        "prompt": "What should be the name of the file you want me to create?",
    },
    {
        "intent": "open_app",
        "patterns": [r"\b(open|launch)\s+(the\s+)?app\b"],
        "validator": lambda q: any(app in q.lower() for app in ["notepad", "browser", "chrome", "edge", "cmd", "powershell", "settings", "calculator", "calc", "explorer"]),
        "prompt": "Which application would you like me to open?",
    }
]

# Suspicious patterns that are almost always Whisper hallucinations
IMPLAUSIBLE_PATTERNS = [
    r"^\w+ing\.$",           # Single gerund: "Howling." "Coding."
    r"^(nice|wow|okay)\!?$", # Single filler word
    r"^[A-Z][a-z]+ [a-z]+ing\.$",  # "Vehicle framing." pattern
]

def _is_plausible_input(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    import re
    for pattern in IMPLAUSIBLE_PATTERNS:
        if re.match(pattern, text, re.IGNORECASE):
            return False
    return True

def _sanitize_history_content(content: str) -> str:
    """Strip debug/status artifacts from stored history before feeding to LLM."""
    if not content:
        return content
    content = re.sub(r'^\[partial\]\s*', '', content)
    content = re.sub(r'^\[agentic\]\s*', '', content)
    content = re.sub(r'\s*\.\.\.\s*\[interrupted\]', '', content)
    content = re.sub(r'\[ACTION:[^\]]+\]\s*', '', content, flags=re.IGNORECASE)  # strip raw action tags from history
    return content.strip()


def _abort_speculative_llm():
    global _spec_llm_active, _spec_llm_generator, _spec_llm_thread, _spec_llm_brain_ref
    _spec_llm_abort.set()
    with speculative_llm_lock:
        if _spec_llm_generator:
            try:
                _spec_llm_generator.close()
            except Exception:
                pass
            _spec_llm_generator = None
        _spec_llm_active = False

        # Rollback conversation history if speculative run added entries
        if _spec_llm_brain_ref is not None:
            hist = _spec_llm_brain_ref.conversation_history
            if len(hist) > _spec_llm_history_snapshot:
                del hist[_spec_llm_history_snapshot:]
            _spec_llm_brain_ref = None


def _start_speculative_llm(transcript: str, agent: "Friday", debug_voice: bool):
    global _spec_llm_thread, _spec_llm_transcript, _spec_llm_generator
    global _spec_llm_active, _spec_llm_done, _spec_llm_abort, _spec_llm_sentences
    global _spec_llm_history_snapshot, _spec_llm_brain_ref

    # Abort any previous speculative run first
    _abort_speculative_llm()

    _spec_llm_transcript = transcript
    _spec_llm_active = True
    _spec_llm_done = False
    _spec_llm_abort.clear()

    # Snapshot conversation history length for rollback
    if agent.brain:
        _spec_llm_history_snapshot = len(agent.brain.conversation_history)
        _spec_llm_brain_ref = agent.brain
    else:
        _spec_llm_history_snapshot = 0
        _spec_llm_brain_ref = None

    # Clear the queue
    while not _spec_llm_sentences.empty():
        try:
            _spec_llm_sentences.get_nowait()
        except queue.Empty:
            break

    def run_worker():
        global _spec_llm_generator, _spec_llm_done, _spec_llm_active
        try:
            if not agent.brain or not agent.brain.enabled:
                return

            generator = agent.brain.stream_sentences(
                transcript,
                memory_hint=" | ".join(agent.memory.recent(5)),
            )

            with speculative_llm_lock:
                if _spec_llm_abort.is_set():
                    return
                _spec_llm_generator = generator

            for sentence in generator:
                if _spec_llm_abort.is_set():
                    break
                sentence = sentence.strip()
                if not sentence:
                    continue
                _spec_llm_sentences.put(sentence)

        except Exception as e:
            if debug_voice:
                print(f"[speculative-llm] Failed: {e}")
        finally:
            with speculative_llm_lock:
                _spec_llm_done = True

    _spec_llm_thread = threading.Thread(target=run_worker, daemon=True, name="spec-llm")
    _spec_llm_thread.start()
    if debug_voice:
        print(f"[speculative-llm] Started speculative LLM prefill for: '{transcript}'")


def run(
    default_voice_mode: bool = True,
    debug_voice: bool = False,
    whisper_model_size: str = "small.en",
    stt_language: str = "en-IN",
    llm_backend: str = "ollama",
    llm_model: str = "auto",
    llm_base_url: str = "http://127.0.0.1:11434",
    llm_timeout: float = 60.0,
    llm_enabled: bool = True,
    always_on: bool = False,
    monitor_enabled: bool = True,
    agentic_enabled: bool = True,   # NEW: enable agentic task routing
    speculative_llm_enabled: bool = False, # NEW: speculative LLM prefill (disabled by default)
) -> None:
    # ── Signal handler for graceful Ctrl+C ────────────────────────────────
    _shutdown_requested = threading.Event()

    def _handle_sigint(signum, frame):
        if _shutdown_requested.is_set():
            print("\nForced exit.")
            sys.exit(1)
        _shutdown_requested.set()
        print("\n\nShutdown requested. Finishing current response... (Ctrl+C again to force)")
        try:
            if 'voice' in locals() and voice and voice._streaming_capture:
                voice._streaming_capture._running = False
        except Exception:
            pass

    signal.signal(signal.SIGINT, _handle_sigint)

    # ── Initialize components ─────────────────────────────────────────────
    persistent_memory = PersistentMemory()

    skill_engine = SkillEngine(
        debug_fn=lambda m: print(f"[skill-debug] {m}") if debug_voice else None
    )

    brain = Brain(
        enabled=llm_enabled and llm_backend == "ollama",
        base_url=llm_base_url,
        model=llm_model,
        timeout_s=llm_timeout,
        debug=debug_voice,
    )

    # ── Inject skill prompt so LLM knows how to invoke tools ──────────────
    brain.skill_prompt = skill_engine.get_skill_prompt()

    # ── Inject long-term memory into LLM conversation history ────────────
    # Load last N turns from disk so Friday remembers previous sessions.
    loaded_turns = persistent_memory.get_history_for_llm()
    if loaded_turns:
        # Merge JSON and JSONL histories uniquely to prevent discarding turns
        existing_history = brain.conversation_history or []
        seen = set()
        merged = []
        for msg in loaded_turns + existing_history:
            key = (msg.get("role"), msg.get("content"))
            if key not in seen:
                seen.add(key)
                merged.append(msg)
        # Cap to brain's max_history * 2 limit to fit LLM context perfectly
        brain.conversation_history = merged[-brain.max_history * 2:]
        for msg in brain.conversation_history:
            msg['content'] = _sanitize_history_content(msg['content'])

        # Enforce strict alternating sequence: user, assistant, user, assistant...
        alternating = []
        expected_role = "user"
        for m in brain.conversation_history:
            role = m.get("role")
            content = m.get("content", "").strip()
            if not content:
                continue
            if role == expected_role:
                alternating.append({"role": role, "content": content})
                expected_role = "assistant" if role == "user" else "user"
            elif role == "user" and expected_role == "assistant":
                # User sent consecutive inputs (e.g. previous turn was interrupted before assistant responded)
                # Keep the latest, most updated user input in history
                if alternating and alternating[-1]["role"] == "user":
                    alternating[-1]["content"] = content

        # Ensure we end with an assistant message so the next user prompt alternates perfectly
        while alternating and alternating[-1]["role"] == "user":
            alternating.pop()

        brain.conversation_history = alternating
        print(f"[memory] Loaded/Merged/Sanitized history from JSON and JSONL ({len(brain.conversation_history)//2} turns).")

    # Inject known facts into system prompt (name, preferences, etc.)
    ctx = persistent_memory.get_context_summary()
    if ctx:
        brain.memory_context = ctx
    brain._build_system_prompt()  # rebuild with skill prompt + memory injected

    agent = Friday(
        name="friday",
        brain=brain,
        always_on=always_on,
        persistent_memory=persistent_memory,
        skills=skill_engine,
    )

    global power_saving_mode
    power_saving_mode = False
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is not None and not bat.power_plugged:
            power_saving_mode = True
            print("\n[power-throttling] System is running on battery (not plugged in).")
            if speculative_llm_enabled:
                print("  -> Throttling background prefill: Speculative LLM DISABLED to extend battery life.")
    except Exception:
        pass

    voice = VoiceIO(
        debug=debug_voice,
        whisper_model_size=whisper_model_size,
        stt_language=stt_language,
    )
    voice.voice_mode = default_voice_mode

    # ── Hook speculative VAD transcript to speculative LLM ─────────────
    if voice._streaming_capture:
        def _on_speculative_transcript(transcript: str):
            if not speculative_llm_enabled:
                return  # Skip speculative LLM entirely if not explicitly enabled
            
            if debug_voice:
                print(f"[speculative] Fired LLM prefill callback: '{transcript[:60]}...'")
            
            # Dynamic battery status checking (enables dynamic charger connect/disconnect re-evaluation!)
            try:
                import psutil
                bat_status = psutil.sensors_battery()
                if bat_status is not None and not bat_status.power_plugged:
                    if debug_voice:
                        print("[power-throttling] Running on battery. Speculative LLM disabled.")
                    return  # Throttled
            except Exception:
                pass

            normalized = transcript.strip().lower()
            if not agent.activated:
                return
            if not _is_plausible_input(transcript):
                if debug_voice:
                    print(f"  [speculative-llm-bypass] Plausibility filter rejected: {transcript!r}")
                return
            if is_wake_command(normalized, agent.name):
                return
            if is_shutdown_command(normalized):
                return
            _start_speculative_llm(transcript, agent, debug_voice)
        voice._streaming_capture.on_speculative_transcript = _on_speculative_transcript

    # ── Tell the LLM about its own voice capabilities ──────────────────
    # Without this, the LLM doesn't know voice is active and may falsely
    # say "I'm in text-based mode" even while speaking aloud to the user.
    if voice.voice_mode and voice.mic_enabled and voice.tts_enabled:
        brain.voice_status = (
            "VOICE STATUS: Voice input AND output are ACTIVE right now. "
            "You ARE speaking aloud to the user through TTS. "
            "You CAN hear the user through the microphone. "
            "Never say you are in text mode — you have full voice capability."
        )
    elif voice.voice_mode and voice.tts_enabled:
        brain.voice_status = (
            "VOICE STATUS: Voice output (TTS) is active. "
            "Microphone is not available — user types, you speak."
        )
    brain._build_system_prompt()  # rebuild with voice status injected

    # Wire TTS into skills so timers/reminders speak aloud when they fire.
    # Uses speak_alarm (non-blocking) so alerts don't starve the TTS pipeline.
    import skills as _skills_mod
    _skills_mod._announce_fn = voice.speak_alarm

    def _make_voice_confirm_fn(voice_io):
        """
        Factory for the agentic confirmation function.
        Mic is muted during agentic execution to prevent echo.
        This wrapper temporarily unmutes → flushes → listens → re-mutes.
        15-second timeout: if user doesn't speak, defaults to 'skip'.
        """
        def _confirm(_prompt=""):
            voice_io.unmute_mic()
            try:
                # Small settle so mic captures cleanly from the start
                import time as _time
                _time.sleep(0.15)
                if voice_io._streaming_capture:
                    voice_io._streaming_capture.flush()
                    voice_io._streaming_capture._listen_aborted = False

                # Use a timeout thread so we don't block forever
                result_box = [None]
                def _listen_worker():
                    result_box[0] = voice_io.listen()

                t = threading.Thread(target=_listen_worker, daemon=True)
                t.start()
                t.join(timeout=15.0)  # 15 seconds max wait

                if t.is_alive():
                    # Timed out — user didn't speak
                    if voice_io._streaming_capture:
                        voice_io._streaming_capture._listen_aborted = True
                    t.join(timeout=2.0)
                    return None
                return result_box[0]
            finally:
                voice_io.mute_mic()
        return _confirm

    # ── Agentic task loop ─────────────────────────────────────────────────
    # Provides voice.listen() as confirm_fn so agentic loop can ask user
    # for confirmation before destructive ops (file_write, run_code, etc.)
    _agentic_loop = AgenticTaskLoop(
        brain=brain,
        skill_engine=skill_engine,
        speak_fn=voice.speak,
        print_fn=print,
        # voice.listen() takes no args, but confirm_fn is called with a prompt string.
        # CRITICAL: mic is muted during agentic execution to prevent echo.
        # We must unmute → listen → re-mute for confirmation to actually work.
        confirm_fn=(
            _make_voice_confirm_fn(voice)
            if (default_voice_mode and voice.mic_enabled)
            else input
        ),
        debug=debug_voice,
    ) if agentic_enabled else None

    # ── Boot messages (mic muted for entire block to prevent echo) ──────
    online_message = (
        f"{agent.name.title()} online. "
        f"Systems restored at {datetime.now().strftime('%I:%M %p')}."
    )
    print(online_message)
    voice.mute_mic()  # ← mute mic for ALL boot speech
    voice.speak(online_message)

    if not always_on:
        standby_message = agent.standby()
        print(standby_message)
        voice.speak(standby_message)
    else:
        agent.activated = True
        ready_msg = f"{agent.name.title()} is ready and listening."
        print(ready_msg)
    voice.unmute_mic()  # ← unmute + flush + settle after all boot TTS done

    # ── Start ProactiveMonitor (after voice is live so alerts can speak) ──
    monitor = ProactiveMonitor(
        announce_fn=voice.speak,
        debug=debug_voice,
    )
    if monitor_enabled:
        monitor.start()

    # ── Status line ───────────────────────────────────────────────────────
    if voice.voice_mode and voice.mic_enabled and voice.tts_enabled:
        status_line = (
            f"Voice: READY "
            f"(mic={voice.mic_backend}, STT={voice.stt_backend}, TTS={voice.tts_effective_backend})"
        )
    elif voice.voice_mode and (voice.mic_enabled or voice.tts_enabled):
        status_line = (
            "Voice: PARTIAL "
            f"(mic={voice.mic_backend}, STT={voice.stt_backend}, "
            f"TTS={voice.tts_effective_backend if voice.tts_enabled else 'off'})"
        )
    elif voice.voice_mode:
        status_line = "Voice: FALLBACK (text-only; mic/TTS unavailable)"
    else:
        status_line = "Voice: DISABLED (text-only mode)"
    print(status_line)

    model_name = brain.active_model or brain.model
    print(f"Brain: {'Ollama (' + model_name + ')' if brain.enabled else 'OFFLINE (rule-based)'}")
    print(f"Skills: {len(skill_engine.skills)} available")
    print("Say 'help' for commands or just talk naturally.\n")
    if debug_voice:
        print("[debug] Debug mode enabled.")


    def _prompt_for_parameter(prompt_text: str, voice_io: "VoiceIO") -> Optional[str]:
        print(f"{agent.name.title()} > {prompt_text}")
        if voice_io.voice_mode and voice_io.tts_enabled:
            voice_io.speak(prompt_text)
            
        if voice_io.voice_mode and voice_io.mic_enabled:
            voice_io.unmute_mic()
            voice_io._last_tts_time = 0.0   # <-- ADD THIS LINE
            try:
                # Small settle so mic captures cleanly from the start
                import time as _time
                _time.sleep(0.15)
                if voice_io._streaming_capture:
                    voice_io._streaming_capture.flush()
                    voice_io._streaming_capture._listen_aborted = False

                # Use a timeout thread so we don't block forever
                result_box = [None]
                def _listen_worker():
                    result_box[0] = voice_io.listen()

                t = threading.Thread(target=_listen_worker, daemon=True)
                t.start()
                t.join(timeout=15.0)  # 15 seconds max wait

                if t.is_alive():
                    # Timed out — user didn't speak
                    if voice_io._streaming_capture:
                        voice_io._streaming_capture._listen_aborted = True
                    t.join(timeout=2.0)
                    return None
                return result_box[0]
            finally:
                voice_io.mute_mic()
        else:
            try:
                return input("You > ")
            except (EOFError, KeyboardInterrupt):
                return None

    # ── Main loop ─────────────────────────────────────────────────────────
    while agent.awake:
        user_input: Optional[str] = None

        # Voice input (streaming Silero VAD + whisper GPU)
        if voice.voice_mode and voice.mic_enabled:
            spoken = voice.listen()
            if spoken:
                if not _is_plausible_input(spoken):
                    if debug_voice:
                        print(f"  [plausibility-rejected (main loop): {spoken!r}]")
                    continue
                print(f"You (voice) > {spoken}")
                user_input = spoken

        # Text input fallback (when mic unavailable or voice mode off)
        if not user_input and (not voice.voice_mode or not voice.mic_enabled):
            try:
                user_input = input("You > ")
            except (EOFError, KeyboardInterrupt):
                print("\nPowering down. Until next time.")
                break

        if _shutdown_requested.is_set():
            print("Shutting down...")
            # Signal capture to exit listen_for_utterance() immediately
            if voice._streaming_capture:
                voice._streaming_capture._running = False
            break

        if not user_input or not user_input.strip():
            continue

        normalized = user_input.strip().lower()

        # ── Shutdown ──────────────────────────────────────────────────────
        if is_shutdown_command(normalized):
            _abort_speculative_llm()
            response = "Powering down. Until next time."
            agent.awake = False
            print(f"{agent.name.title()} > {response}")
            voice.speak(response)
            persistent_memory.log_interaction(user_input, response)
            break

        # ── Wake word (standby mode) ──────────────────────────────────────
        if not agent.activated:
            _abort_speculative_llm()
            if is_wake_command(normalized, agent.name):
                response = agent.wake()
                print(f"{agent.name.title()} > {response}")
                voice.speak(response)
            else:
                response = f"In standby. Say 'wake up {agent.name}' to activate."
                print(f"{agent.name.title()} > {response}")
                voice.speak(response)
            continue

        # ── Already active, wake word repeated ───────────────────────────
        if is_wake_command(normalized, agent.name):
            _abort_speculative_llm()
            response = "I am already online and listening."
            print(f"{agent.name.title()} > {response}")
            voice.speak(response)
            continue

        # ── Voice mode toggles ────────────────────────────────────────────
        if normalized in ("voice on", "enable voice", "turn voice on"):
            _abort_speculative_llm()
            voice.voice_mode = True
            response = "Voice mode enabled."
            print(f"{agent.name.title()} > {response}")
            voice.speak(response)
            continue

        if normalized in ("voice off", "disable voice", "turn voice off"):
            _abort_speculative_llm()
            voice.voice_mode = False
            response = "Voice mode disabled."
            print(f"{agent.name.title()} > {response}")
            continue

        # ── Pre-LLM intent detection (instant, no LLM needed) ────────────
        # Intercepts obvious commands like "open notepad", "close chrome",
        # "volume up" etc. before they reach Qwen3 — which often fails to
        # emit [ACTION:...] tags and just responds conversationally.
        direct = detect_direct_intent(user_input)
        if direct is not None:
            _abort_speculative_llm()
            _handle_direct_skill(
                agent, voice, user_input, persistent_memory,
                skill_engine, direct,
            )
        # ── LLM or rule-based response ────────────────────────────────────
        elif agent.brain and agent.brain.enabled:
            # Route to agentic loop for multi-step tasks
            if _agentic_loop is not None and is_agentic_task(user_input, brain=agent.brain):
                _abort_speculative_llm()
                
                # Verify required parameters before executing
                for item in REQUIRED_PARAMS:
                    matches = False
                    for pattern in item["patterns"]:
                        if re.search(pattern, user_input, re.IGNORECASE):
                            matches = True
                            break
                    if matches and not item["validator"](user_input):
                        clarification = _prompt_for_parameter(item["prompt"], voice)
                        if clarification and clarification.strip():
                            user_input = f"{user_input}: {clarification}"
                            print(f"[parameter-clarified] Updated task: {user_input}")
                            
                _handle_agentic_response(
                    agent, voice, user_input, persistent_memory, _agentic_loop
                )
            else:
                _handle_brain_response(agent, voice, user_input, persistent_memory)
        else:
            _abort_speculative_llm()
            response = agent.handle(user_input)
            print(f"{agent.name.title()} > {response}")
            voice.speak(response)
            persistent_memory.log_interaction(user_input, response)

        if not agent.awake:
            break

    # ── Cleanup ───────────────────────────────────────────────────────────
    monitor.stop()
    voice.shutdown()
    print("FRIDAY shut down cleanly.")


def _handle_brain_response(
    agent: "Friday",
    voice: "VoiceIO",
    user_input: str,
    persistent_memory: "PersistentMemory",
) -> None:
    """
    Stream LLM response sentence-by-sentence with pipelined TTS.

    Skills are dispatched IMMEDIATELY as their [ACTION:...] tags are
    yielded by the LLM, so tasks complete before or during speech,
    not after all speech finishes.

    Barge-in recovery is ITERATIVE (not recursive) — prevents RecursionError
    and the post-barge-in crash when the user interrupts multiple times.
    """
    global _spec_llm_brain_ref
    current_input = user_input
    MAX_BARGE_IN_CHAIN = 5  # safety cap — prevents infinite loop on stuck mic

    for _turn in range(MAX_BARGE_IN_CHAIN + 1):
        # ── Per-turn state (use dict so closure captures by reference) ────
        _state: dict = {"full_response": "", "skill_results": [], "sentences": []}

        # Check if we can reuse the speculative LLM run
        use_speculative = False
        with speculative_llm_lock:
            if _spec_llm_active and _spec_llm_transcript:
                if _are_semantically_similar(current_input, _spec_llm_transcript) and _spec_llm_history_snapshot == len(agent.brain.conversation_history):
                    use_speculative = True

        if use_speculative:
            # Clear brain_ref so _abort_speculative_llm() won't roll back
            # the conversation history that stream_sentences() committed —
            # we're ADOPTING this run, so the history is valid.
            with speculative_llm_lock:
                _spec_llm_brain_ref = None

            if agent.brain and agent.brain.debug:
                print("\n[speculative-llm] REUSING active speculative LLM run!")

            # Helper generator to consume the speculative queue
            class SpeculativeGenerator:
                def __iter__(self):
                    return self

                def __next__(self):
                    while True:
                        try:
                            return _spec_llm_sentences.get_nowait()
                        except queue.Empty:
                            with speculative_llm_lock:
                                if _spec_llm_done and _spec_llm_sentences.empty():
                                    raise StopIteration
                            # Block wait briefly
                            try:
                                return _spec_llm_sentences.get(timeout=0.05)
                            except queue.Empty:
                                continue

                def close(self):
                    # Stop the generator but do NOT roll back history —
                    # brain_ref was already cleared above, so
                    # _abort_speculative_llm() is history-safe.
                    _abort_speculative_llm()

            sentence_gen = SpeculativeGenerator()
        else:
            _abort_speculative_llm()
            with speculative_llm_lock:
                _spec_llm_brain_ref = None
            sentence_gen = agent.brain.stream_sentences(
                current_input,
                memory_hint=" | ".join(agent.memory.recent(5)),
            )

        print(f"{agent.name.title()} > ", end="", flush=True)

        def _sentence_stream(state=_state, sg=sentence_gen):
            """Yield speakable sentences; execute skills instantly."""
            for sentence in sg:
                if voice._playback_stop.is_set():
                    break
                sentence = sentence.strip()
                if not sentence:
                    continue

                state["full_response"] = (
                    state["full_response"] + " " + sentence
                ).strip()

                # Execute skill IMMEDIATELY if [ACTION:] tag found
                if "[action" in sentence.lower():
                    cleaned, results = agent.process_skill_actions(sentence)
                    if results:
                        for result in results:
                            print(f"\n  [Skill] {result}")
                            state["skill_results"].append(result)
                            # Truncate long error messages (file paths etc.)
                            # before TTS — prevents 7-second error reads.
                            spoken = result if len(result) <= 120 else result[:117] + "..."
                            yield spoken
                    elif cleaned:
                        # Tag was malformed — no match. Still yield the text
                        # so the user hears something instead of silence.
                        state["sentences"].append(cleaned)
                        yield cleaned
                else:
                    state["sentences"].append(sentence)
                    yield sentence

        # 3-thread pipeline: LLM feeder | TTS generator | Player — all parallel
        voice.speak_pipeline(_sentence_stream())

        # Close the generator immediately on barge-in to stop Ollama generating in the background
        if voice._barge_in_speech_pending:
            try:
                sentence_gen.close()
            except Exception:
                pass

        full_response = _state["full_response"]
        skill_results = _state["skill_results"]

        # ── Action tag validation retry ────────────────────────────────────
        # If LLM emitted [ACTION but nothing executed (malformed tag),
        # do ONE corrective retry with a format reminder.
        # Only retry if no sentences were spoken — otherwise user already
        # heard partial text and a retry would double-speak.
        if (
            "[action" in full_response.lower()
            and not skill_results
            and not _state["sentences"]
            and _turn == 0
        ):
            is_valid, error = agent.skills.validate_action_tags(full_response)
            if not is_valid:
                print(f"\n  [tag-fix] Malformed tag detected: {error}")
                retry_prompt = (
                    f"Your previous response had a malformed action tag. "
                    f"Error: {error}. "
                    f"Correct format: [ACTION:skill_name:arguments]\n"
                    f"Original request: {current_input}\n"
                    f"Try again with the correct tag format."
                )
                # Use generate_simple (not generate) to avoid printing raw
                # tokens to terminal — this is an internal retry, not user-facing.
                retry_resp = agent.brain.generate_simple(
                    retry_prompt, max_tokens=200, temperature=0.3
                )
                if retry_resp and "[action:" in retry_resp.lower():
                    cleaned, results = agent.process_skill_actions(retry_resp)
                    for result in results:
                        print(f"\n  [Skill retry] {result}")
                        skill_results.append(result)
                    if cleaned:
                        voice.speak(cleaned)

        # ── Memory for this turn ─────────────────────────────────────────
        agent.memory.add(current_input)

        was_interrupted = voice._barge_in_speech_pending
        if full_response or was_interrupted:
            if was_interrupted:
                # Reconstruct the actually spoken response
                spoken_text = " ".join(voice.played_sentences).strip()
                voice.played_sentences = []  # clear after reading to avoid accumulation
                if spoken_text:
                    spoken_response = spoken_text  # Clean spoken text for LLM conversation history
                    log_response = f"{spoken_text} ... [interrupted]"
                else:
                    spoken_response = ""
                    log_response = "[interrupted before speaking]"

                # Correct the assistant entry in LLM's conversation history
                history_updated = False
                if agent.brain and agent.brain.conversation_history:
                    if len(agent.brain.conversation_history) >= 2:
                        last_turn = agent.brain.conversation_history[-1]
                        prev_turn = agent.brain.conversation_history[-2]
                        if prev_turn["role"] == "user" and prev_turn["content"] == current_input.strip():
                            history_updated = True

                if history_updated:
                    agent.brain.conversation_history[-1]["content"] = _sanitize_history_content(spoken_response)
                else:
                    if agent.brain:
                        agent.brain.conversation_history.append({
                            "role": "user", "content": current_input.strip()
                        })
                        agent.brain.conversation_history.append({
                            "role": "assistant", "content": _sanitize_history_content(spoken_response)
                        })
                if agent.brain:
                    agent.brain.save_history()

                persistent_memory.log_interaction(
                    current_input, f"[partial] {log_response}"
                )
            else:
                persistent_memory.log_interaction(current_input, full_response)

        for result in skill_results:
            if result:
                persistent_memory.log_interaction("[skill_result]", result)

        # ── Barge-in recovery: listen then loop (never recurse) ──────────
        if not was_interrupted:
            _abort_speculative_llm()
            break  # normal turn complete

        recovered = voice.listen()
        voice._barge_in_speech_pending = False

        if not recovered or not recovered.strip():
            _abort_speculative_llm()
            break  # barge-in but silence — done

        # Check plausibility of barge-in input to prevent hallucination propagation
        if not _is_plausible_input(recovered):
            if voice.debug:
                print(f"  [plausibility-rejected (barge-in recovery): {recovered!r}]")
            _abort_speculative_llm()
            break

        print(f"You (voice/barge-in) > {recovered}")
        current_input = recovered  # next iteration processes this input


def _handle_direct_skill(
    agent: "Friday",
    voice: "VoiceIO",
    user_input: str,
    persistent_memory: "PersistentMemory",
    skill_engine: "SkillEngine",
    intent: tuple[str, str, str],
) -> None:
    """
    Handle a command intercepted by the pre-LLM intent detector.
    Executes the skill directly, speaks the result, logs to memory.
    Zero LLM latency — instant response.
    """
    skill_name, skill_args, spoken_intro = intent
    fn = skill_engine.skills.get(skill_name)
    if fn is None:
        voice.speak(f"I don't know how to do that yet.")
        return

    print(f"[direct] {skill_name}({skill_args})")

    # Speak the intro ("Opening Notepad for you, sir.") BEFORE executing
    # so the user gets instant audio feedback while the skill runs.
    if spoken_intro:
        voice.speak(spoken_intro)

    # Execute the skill
    try:
        result = fn(skill_args)
    except Exception as exc:
        result = f"Skill error: {exc}"

    print(f"[direct] Result: {result}")

    # If the skill returned meaningful text (not just "Opened X"), speak it
    # This handles skills like weather, calculator, system_info that return data
    if result and not any(
        result.startswith(p) for p in ("Opened ", "Closed ", "Volume ", "Copied ")
    ):
        voice.speak(result)

    # Log to memory & LLM history
    agent.memory.add(user_input)
    response_text = spoken_intro + " " + result if spoken_intro else result
    persistent_memory.log_interaction(user_input, response_text.strip())

    if agent.brain:
        agent.brain.conversation_history.append({"role": "user", "content": user_input})
        agent.brain.conversation_history.append({"role": "assistant", "content": _sanitize_history_content(response_text.strip())})
        agent.brain.conversation_history = agent.brain.conversation_history[-agent.brain.max_history * 2:]
        agent.brain.save_history()


def _handle_agentic_response(
    agent: "Friday",
    voice: "VoiceIO",
    user_input: str,
    persistent_memory: "PersistentMemory",
    agentic_loop: "AgenticTaskLoop",
) -> None:
    """
    Route a multi-step request through the agentic Plan→Execute→Verify→Done loop.

    If the loop returns empty (task was pure Q&A, no steps planned), falls back
    to the normal streaming brain response so the user always gets an answer.
    """
    print(f"[agentic] Routing to agentic loop: {user_input[:80]}")

    # Mute mic during agentic execution to prevent false wake words
    # from confirmation prompts being misheard as new commands.
    voice.mute_mic()
    try:
        summary = agentic_loop.run(user_input)
    except Exception as exc:
        print(f"[agentic] Loop error: {exc}")
        summary = ""
    finally:
        voice.unmute_mic()

    if not summary:
        # Agentic loop found no steps — treat as normal LLM request
        print("[agentic] No steps planned — falling back to normal response")
        _handle_brain_response(agent, voice, user_input, persistent_memory)
        return

    # Log the agentic task + summary to persistent memory & LLM history
    agent.memory.add(user_input)
    persistent_memory.log_interaction(user_input, f"[agentic] {summary}")

    if agent.brain:
        agent.brain.conversation_history.append({"role": "user", "content": user_input})
        agent.brain.conversation_history.append({"role": "assistant", "content": _sanitize_history_content(f"[agentic] {summary}")})
        agent.brain.conversation_history = agent.brain.conversation_history[-agent.brain.max_history * 2:]
        agent.brain.save_history()



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FRIDAY — Advanced AI Voice Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py                  # Voice mode, Ollama LLM\n"
            "  python main.py --always-on      # Skip standby, always listen\n"
            "  python main.py --no-voice       # Text-only mode\n"
            "  python main.py --debug-voice    # Show diagnostics\n"
            "  python main.py --no-llm         # Rule-based, no Ollama needed\n"
            "  python main.py --no-agentic     # Disable agentic loop (single-turn only)\n"
        ),
    )
    parser.add_argument("--voice",    dest="voice", action="store_true",  help="Voice mode (default).")
    parser.add_argument("--no-voice", dest="voice", action="store_false", help="Text-only mode.")
    parser.add_argument("--always-on",  action="store_true", help="Skip standby, always listening.")
    parser.add_argument("--debug-voice", action="store_true", help="Print voice diagnostics.")
    parser.add_argument(
        "--whisper-model", default="small.en",
        help="Whisper model size: tiny.en, base.en, small.en (default), medium.en",
    )
    parser.add_argument(
        "--stt-language", default="en-IN",
        help="STT language locale (en-IN, en-US, hi-IN).",
    )
    parser.add_argument(
        "--llm-backend", choices=["ollama", "none"], default="ollama",
        help="LLM backend.",
    )
    parser.add_argument("--llm-model",    default="auto",                    help="Ollama model name.")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:11434", help="Ollama server URL.")
    parser.add_argument("--llm-timeout",  type=float, default=60.0,          help="LLM timeout (seconds).")
    parser.add_argument("--no-llm",       action="store_true",               help="Disable LLM.")
    parser.add_argument("--list-skills",  action="store_true",               help="List skills and exit.")
    parser.add_argument("--no-monitor",   action="store_true",               help="Disable ProactiveMonitor.")
    parser.add_argument("--no-agentic",   action="store_true",               help="Disable agentic loop.")
    parser.add_argument("--speculative-llm", action="store_true",            help="Enable speculative LLM background prefill.")
    parser.set_defaults(voice=True, speculative_llm=False)

    args = parser.parse_args(sys.argv[1:])

    if args.list_skills:
        print(SkillEngine().list_skills())
        sys.exit(0)

    run(
        default_voice_mode=args.voice,
        debug_voice=args.debug_voice,
        whisper_model_size=args.whisper_model,
        stt_language=args.stt_language,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        llm_timeout=args.llm_timeout,
        llm_enabled=not args.no_llm,
        always_on=args.always_on,
        monitor_enabled=not args.no_monitor,
        agentic_enabled=not args.no_agentic,
        speculative_llm_enabled=args.speculative_llm,
    )