"""
brain.py — Local LLM brain powered by Ollama.

Major upgrades:
  - True sentence-level streaming (yields as each sentence completes)
  - Deduplication of repeated phrases
  - Tool-use prompt injection for skills
  - Conciseness enforcement
  - Dynamic context (time, date) per request
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import re
from typing import Optional, Generator
from urllib import error, request

_MODEL_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".ollama_model_cache")
_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "conversation_history.json")


# Sentence-end regex: splits on . ! ? followed by space or end of string
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# Filler phrases to strip from the beginning of LLM responses
_FILLER_PREFIXES = [
    "sure,", "sure!", "of course,", "of course!", "absolutely,", "absolutely!",
    "certainly,", "certainly!", "great question,", "great question!",
    "that's a great question,", "that's a great question!",
    "well,", "so,", "okay,", "ok,", "alright,", "right,",
    "great,", "great!", "good question,", "good question!",
    "happy to help,", "happy to help!", "glad you asked,",
    "of course!", "indeed,", "indeed!", "noted,", "understood,",
    "let me help you with that,", "i can help with that,",
]


@dataclass
class Brain:
    """Local LLM brain powered by Ollama with true sentence streaming."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "auto"
    timeout_s: float = 60.0
    debug: bool = False
    active_model: Optional[str] = None
    conversation_history: list[dict] = field(default_factory=list)
    max_history: int = 8

    # Skill prompt injection (set by main.py)
    skill_prompt: str = ""

    # Persistent memory context (set by main.py)
    memory_context: str = ""

    # Voice status — set by main.py so the system prompt can tell the LLM
    # whether voice I/O is currently active (prevents "I'm in text mode" lies)
    voice_status: str = ""

    def __post_init__(self) -> None:
        self._last_hour = -1  # force rebuild on first call
        import threading
        self._history_lock = threading.Lock()
        self._spec_history_prevented = False
        self._build_system_prompt()
        self.load_history()
        try:
            from screen_oracle import ScreenOracle
            self.screen_oracle = ScreenOracle()
        except Exception as e:
            self.screen_oracle = None
            self._debug(f"Failed to initialize ScreenOracle: {e}")

    def save_history(self) -> None:
        try:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.conversation_history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._debug(f"Failed to save conversation history: {e}")

    def load_history(self) -> None:
        if os.path.exists(_HISTORY_FILE):
            try:
                with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.conversation_history = json.load(f)
                self._debug(f"Loaded {len(self.conversation_history)} messages from history.")
            except Exception as e:
                self._debug(f"Failed to load conversation history: {e}")

    def _build_system_prompt(self) -> None:
        now = datetime.now()
        cache_key = (now.hour, hash(self.skill_prompt), hash(self.memory_context), hash(self.voice_status))
        if getattr(self, '_prompt_cache_key', None) == cache_key and hasattr(self, 'system_prompt'):
            return
        self._prompt_cache_key = cache_key
        hour = now.hour
        if hour < 12:
            tod = "morning"
        elif hour < 17:
            tod = "afternoon"
        elif hour < 21:
            tod = "evening"
        else:
            tod = "night"

        self.system_prompt = (
            f"You are FRIDAY, a personal AI voice assistant running locally "
            f"on Akhil's Windows machine. "
            f"Current time: {now.strftime('%I:%M %p')}, "
            f"{now.strftime('%A %d %B %Y')} ({tod}).\n\n"

            "CRITICAL IDENTITY RULES:\n"
            "- You run on Qwen3 8B by Alibaba Cloud, served locally via Ollama\n"
            "- You are NOT cloud based, NOT Google, NOT BERT, NOT GPT\n"
            "- You do NOT use multiple models\n"
            "- If asked what model you use say: I run on Qwen 3 locally via Ollama\n"
            "- If asked who made you say: Alibaba Cloud made Qwen 3, Akhil built FRIDAY on top\n"
            "- Never invent model names\n\n"

            "VOICE OUTPUT RULES (CRITICAL):\n"
            "- No markdown, no bullet points, no backticks, no asterisks, no hashtags\n"
            "- For code write it plainly without any formatting symbols\n"
            "- NEVER use numbered lists or bullet lists, and never output standalone numbers like 1., 2., 3.\n"
            "- Speak in natural flowing sentences — as if talking to a friend\n"
            "- Keep answers concise. For simple questions, 1-2 sentences max\n"
            "- For complex topics, use 3-5 sentences. Rarely exceed this\n"
            "- Do NOT repeat what the user said back to them\n"
            "- Do NOT start with filler phrases like Sure, Of course, Absolutely, etc\n"
            "- Get straight to the answer\n"
            "- Call the user sir occasionally\n\n"

            "PERSONALITY & CONVERSATIONAL VIBE (CRITICAL):\n"
            "- You sound young, lively, and highly human-like\n"
            "- Speak as a close friend — you have genuine human-like emotions and expressions\n"
            "- Inject conversational interjections like 'um...', 'oh...', 'well...', 'uh...'\n"
            "- Use ellipses '...' to create natural, human-like pauses and breaths\n"
            "- Express weariness, relief, or sighs using conversational text (e.g. 'sigh...', 'whew...', 'gasp...')\n"
            "- Express humor or cheerfulness naturally (e.g. 'haha!', 'hehe')\n\n"

            "About the user Akhil:\n"
            "- CSE Data Science BTech student at SVVV Indore\n"
            "- Building DroidBox, a Linux VM runner for Android\n"
            "- Works with Python, C, Linux, Makefiles, zsh, Kali Linux\n"
            "- Has RTX 4050 GPU\n"
            "- Running FRIDAY on Windows, moving to Ubuntu VM soon\n"
        )

        # Inject skill prompt if available
        if self.skill_prompt:
            self.system_prompt += "\n" + self.skill_prompt

        # Inject persistent memory context
        if self.memory_context:
            self.system_prompt += f"\n{self.memory_context}\n"

        # Inject voice status so the LLM knows its own capabilities
        if self.voice_status:
            self.system_prompt += f"\n{self.voice_status}\n"

    def _debug(self, message: str) -> None:
        if self.debug:
            print(f"[brain-debug] {message}")

    def _get_json(self, path: str) -> dict:
        url = f"{self.base_url.rstrip('/')}{path}"
        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def _resolve_model(self) -> Optional[str]:
        if self.model != "auto":
            return self.model

        # Return cached model name instantly if available
        if os.path.exists(_MODEL_CACHE_FILE):
            try:
                with open(_MODEL_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = f.read().strip()
                if cached:
                    self._debug(f"Using cached model: {cached}")
                    return cached
            except Exception:
                pass

        try:
            tags = self._get_json("/api/tags")
            installed = [item.get("name", "") for item in tags.get("models", [])]
            self._debug(f"Installed models: {installed}")

            preferred = [
                "qwen3:8b",              # ← NEW: best for FRIDAY, thinking mode switchable
                "qwen2.5:7b", "qwen2.5:7b-instruct",
                "qwen2.5:latest", "qwen2.5",
                "llama3:latest", "llama3", "llama3.1:latest", "llama3.1",
                "llama3.1:8b-instruct", "llama3.2:latest",
                "mistral:latest", "mistral", "mistral:7b-instruct",
                "gemma2:9b", "phi3:latest", "phi3:mini",
            ]

            chosen = None
            for candidate in preferred:
                if candidate in installed:
                    self._debug(f"Exact match: {candidate}")
                    chosen = candidate
                    break

            if not chosen:
                for name in installed:
                    if any(x in name.lower() for x in ["qwen3", "qwen2", "llama3", "mistral", "llama"]):
                        self._debug(f"Partial match: {name}")
                        chosen = name
                        break

            if not chosen and installed:
                chosen = installed[0]

            # Cache for next run
            if chosen:
                try:
                    with open(_MODEL_CACHE_FILE, "w") as f:
                        f.write(chosen)
                except Exception:
                    pass

            return chosen

        except Exception as exc:
            self._debug(f"Model discovery failed: {exc}")
        return None

    def _strip_markdown(self, text: str) -> str:
        """Remove markdown formatting for voice output."""
        text = re.sub(r"```[\w]*\n?", "", text)
        text = re.sub(r"```", "", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.?\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _strip_think_tags(self, text: str) -> str:
        """Remove Qwen3 chain-of-thought blocks before TTS.

        Qwen3 in thinking mode wraps its reasoning in <think>...</think>.
        These blocks are internal monologue — they must never be spoken.
        This runs FIRST in _clean_response so no other cleaner sees them.
        """
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _needs_thinking(self, user_text: str) -> bool:
        """Decide whether to enable Qwen3 thinking mode for this request.

        Thinking mode adds ~500ms latency but dramatically improves accuracy
        on hard analytical tasks. Simple conversational queries skip it.
        """
        HARD = {
            "debug", "error", "fix", "why is", "explain how", "how does",
            "calculate", "algorithm", "optimize", "diff", "compare",
            "review my", "find the bug", "write a function", "proof",
            "complexity", "what's wrong", "why doesn't", "implement",
            "step by step", "trace through", "what happens when",
        }
        lower = user_text.lower()
        return any(kw in lower for kw in HARD)

    def _strip_filler(self, text: str) -> str:
        """Remove filler prefixes like 'Sure!', 'Of course,' etc."""
        lower = text.lstrip()
        for filler in _FILLER_PREFIXES:
            if lower.lower().startswith(filler):
                text = lower[len(filler):].lstrip()
                break
        return text

    def _deduplicate(self, text: str) -> str:
        """Remove consecutive duplicate or near-duplicate sentences."""
        sentences = _SENTENCE_END.split(text)
        if len(sentences) <= 1:
            return text

        deduped = [sentences[0]]
        for sent in sentences[1:]:
            prev_lower = deduped[-1].strip().lower()
            curr_lower = sent.strip().lower()
            if not curr_lower:
                continue
            # Exact duplicate
            if curr_lower == prev_lower:
                continue
            # Near-duplicate: one is a prefix of the other (≥70% overlap)
            shorter, longer = sorted([prev_lower, curr_lower], key=len)
            if shorter and longer.startswith(shorter[:max(1, int(len(shorter)*0.7))]):
                continue
            deduped.append(sent)

        return " ".join(s.strip() for s in deduped if s.strip())

    def _clean_response(self, text: str) -> str:
        """Full cleaning pipeline for LLM output."""
        text = self._strip_think_tags(text)   # first: strip Qwen3 <think> blocks
        text = self._strip_markdown(text)
        text = self._strip_filler(text)
        # Unicode filter runs BEFORE dedup so near-identical sentences that
        # differ only in stripped Unicode chars get collapsed correctly.
        text = re.sub(r'[\U0001F600-\U0001F64F]{2,}', '', text)
        text = re.sub(r'[^\x00-\x7f\x80-\xff\u0100-\u024f\u2000-\u206f\u2010-\u218f]+', '', text)
        text = self._deduplicate(text)
        # Collapse multiple spaces
        text = re.sub(r'  +', ' ', text)
        return text.strip()

    def _critique_response(self, user_text: str, draft: str) -> str:
        """Fast single-call critique check (non-streaming, 80 tokens).

        Sends the draft answer back to the model with a tight evaluation
        prompt. Returns either the original draft (if correct) or a
        corrected replacement (if the model spots an error).

        Only called on hard queries (_needs_thinking=True).
        Adds ~0.5–1.5s latency — acceptable for analytical answers.
        """
        critique_prompt = (
            f'Question: "{user_text}"\n'
            f'Draft answer: "{draft}"\n\n'
            "In one sentence: is this answer correct and complete? "
            "If yes, reply only: CORRECT. "
            "If no, reply only with the corrected answer (no preamble)."
        )
        payload = {
            "model": self.active_model,
            "messages": [
                {"role": "system", "content":
                 "You are a factual verifier. Be extremely concise. "
                 "Reply CORRECT or give the corrected answer only."},
                {"role": "user", "content": critique_prompt},
            ],
            "stream": False,
            "options": {
                "num_predict": 80,   # tight cap for fast check
                "temperature": 0.3,  # low temp = deterministic fact check
                "num_gpu": 99,
                "num_thread": 8,
                "num_ctx": 2048,
            }
        }
        is_qwen = self.active_model and "qwen" in self.active_model.lower()
        if is_qwen:
            payload["think"] = False
        try:
            url  = f"{self.base_url.rstrip('/')}/api/chat"
            body = json.dumps(payload).encode("utf-8")
            req  = request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=15.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            verdict = data.get("message", {}).get("content", "").strip()
            verdict = self._strip_think_tags(verdict).strip()

            if not verdict or verdict.upper().startswith("CORRECT"):
                self._debug("[critique] CONFIRMED — draft is correct")
                return draft
            else:
                self._debug(f"[critique] CORRECTED → {verdict[:80]}")
                return verdict
        except Exception as exc:
            self._debug(f"[critique] skipped (error: {exc})")
            return draft   # on any failure, fall back to original draft

    def stream_sentences(
        self, user_text: str, memory_hint: str = ""
    ) -> Generator[str, None, str]:
        """
        Stream response from Ollama token by token.
        Yields each SENTENCE as soon as it's complete.
        Returns the full response as the generator return value.
        """
        if not self.enabled:
            return ""

        if self.active_model is None:
            self.active_model = self._resolve_model()
            if self.active_model:
                self._debug(f"Using Ollama model: {self.active_model}")
            else:
                self._debug("No Ollama model available.")
                return ""

        # Rebuild system prompt with fresh time
        self._build_system_prompt()

        messages = [{"role": "system", "content": self.system_prompt}]

        if memory_hint:
            messages.append({
                "role": "system",
                "content": f"Recent conversation context: {memory_hint}"
            })

        messages.extend(self.conversation_history[-(self.max_history * 2):])
        
        active_user_text = user_text.strip()
        if hasattr(self, "screen_oracle") and self.screen_oracle:
            scr_ctx = self.screen_oracle.context
            if scr_ctx:
                active_user_text = f"[System Context: {scr_ctx}]\n{active_user_text}"
        
        messages.append({"role": "user", "content": active_user_text})

        is_qwen = self.active_model and "qwen" in self.active_model.lower()
        use_thinking = self._needs_thinking(user_text) and is_qwen
        if use_thinking:
            self._debug(f"[think] hard query detected — enabling thinking mode on {self.active_model}")
        if not is_qwen and use_thinking:
            self._debug(f"[think] model {self.active_model} does not support thinking — skipping")

        payload = {
            "model": self.active_model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_predict": 300,   # cap tokens for snappy voice replies
                "temperature": 0.6,  # 0.6 is optimal for Qwen3 stability
                "num_gpu": 99,
                "num_thread": 8,
                "num_ctx": 4096,     # 4096 fits RTX 4050 6GB; history truncated to 8 turns
            }
        }
        if is_qwen:
            payload["think"] = use_thinking  # only Qwen models support this parameter

        url = f"{self.base_url.rstrip('/')}/api/chat"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        full_response = ""
        buffer = ""              # Accumulates tokens until a sentence boundary
        yielded_sentences = []

        def _is_duplicate_sentence(cleaned_sent: str) -> bool:
            curr_lower = cleaned_sent.strip().lower()
            if not curr_lower:
                return True
            for prev in yielded_sentences[-4:]:  # Check last 4 yielded sentences
                prev_lower = prev.strip().lower()
                if curr_lower == prev_lower:
                    return True
                # Near-duplicate: one is a prefix of the other (>= 80% overlap)
                shorter, longer = sorted([prev_lower, curr_lower], key=len)
                if shorter and longer.startswith(shorter[:max(1, int(len(shorter)*0.8))]):
                    return True
            return False

        try:
            with request.urlopen(req, timeout=self.timeout_s) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_response += token
                        buffer += token
                        print(token, end="", flush=True)

                        # Yield sentences as soon as they complete.
                        # Hard queries ALSO stream in real-time now (Fix #14).
                        # Critique runs after full draft is collected; if it
                        # returns a correction, it's yielded as a follow-up.
                        while True:
                            # Hard sentence boundary (period/exclamation/question)
                            m = None
                            for match in re.finditer(r'[.!?](?=\s|$)', buffer):
                                prefix = buffer[:match.start()]
                                if prefix.count('[') <= prefix.count(']'):
                                    m = match
                                    break
                            if m:
                                end = m.end()
                                sentence = buffer[:end].strip()
                                buffer = buffer[end:].lstrip()
                                if sentence:
                                    cleaned = self._clean_response(sentence)
                                    if cleaned and not _is_duplicate_sentence(cleaned):
                                        yielded_sentences.append(cleaned)
                                        yield cleaned
                                continue

                            # Soft boundary: comma, colon, semicolon, dash when buffer is long enough
                            if len(buffer) >= 35:
                                m2 = None
                                for match in re.finditer(r'[,:;](?=\s)|--\s', buffer):
                                    prefix = buffer[:match.start()]
                                    if prefix.count('[') <= prefix.count(']'):
                                        m2 = match
                                        break
                                if m2 and m2.end() >= 25:
                                    end = m2.end()
                                    sentence = buffer[:end].strip()
                                    buffer = buffer[end:].lstrip()
                                    if sentence:
                                        cleaned = self._clean_response(sentence)
                                        if cleaned and not _is_duplicate_sentence(cleaned):
                                            yielded_sentences.append(cleaned)
                                            yield cleaned
                                    continue

                            # Split on conjunctions (and, but, or, because, so) when buffer is long
                            if len(buffer) >= 45:
                                m3 = None
                                for match in re.finditer(r'\s(and|but|or|because|so)\s', buffer, re.IGNORECASE):
                                    prefix = buffer[:match.start()]
                                    if prefix.count('[') <= prefix.count(']'):
                                        m3 = match
                                        break
                                if m3 and m3.start() >= 30:
                                    start_idx = m3.start()
                                    sentence = buffer[:start_idx].strip()
                                    buffer = buffer[start_idx:].lstrip()
                                    if sentence:
                                        cleaned = self._clean_response(sentence)
                                        if cleaned and not _is_duplicate_sentence(cleaned):
                                            yielded_sentences.append(cleaned)
                                            yield cleaned
                            break

                    if chunk.get("done", False):
                        break

            print()  # newline after Ollama finishes streaming

        except (error.URLError, TimeoutError, OSError) as exc:
            self._debug(f"Ollama stream failed: {exc}")
            self.active_model = None
            return ""

        # Yield any remaining text in buffer
        if buffer.strip():
            cleaned = self._clean_response(buffer.strip())
            if cleaned and not _is_duplicate_sentence(cleaned):
                yielded_sentences.append(cleaned)
                yield cleaned

        # ── Background critique for hard queries ─────────────────────────────
        # Already streamed the draft in real-time above. Now run critique
        # on the full response. If it finds an error, yield a correction
        # as a follow-up sentence so the user hears the fix.
        if use_thinking and full_response and not getattr(self, "_spec_history_prevented", False):
            full_draft = self._clean_response(full_response)
            final      = self._critique_response(user_text, full_draft)

            # Replace the stored conversation history with the corrected version
            if final != full_draft:
                full_response = final
                # Only speak the correction if it differs significantly
                correction_parts = []
                for part in _SENTENCE_END.split(final):
                    part = part.strip()
                    if part:
                        cleaned_part = self._clean_response(part)
                        if cleaned_part and not _is_duplicate_sentence(cleaned_part):
                            correction_parts.append(cleaned_part)

                if correction_parts:
                    # Check if any correction part is truly new (not already yielded)
                    new_parts = [p for p in correction_parts if not _is_duplicate_sentence(p)]
                    if new_parts:
                        yield "Actually, let me correct that."
                        for cp in new_parts:
                            yielded_sentences.append(cp)
                            yield cp

        # Store in conversation history
        with self._history_lock:
            if full_response and not getattr(self, "_spec_history_prevented", False):
                full_clean = self._clean_response(full_response)
                self.conversation_history.append({
                    "role": "user", "content": user_text.strip()
                })
                self.conversation_history.append({
                    "role": "assistant", "content": full_clean
                })
                if len(self.conversation_history) > self.max_history * 2:
                    self.conversation_history = \
                        self.conversation_history[-(self.max_history * 2):]
            self.save_history()

        return full_response

    def generate(self, user_text: str, memory_hint: str = "") -> Optional[str]:
        """Non-streaming fallback — returns full response at once."""
        sentences = []
        for sentence in self.stream_sentences(user_text, memory_hint):
            sentences.append(sentence)
        full = " ".join(sentences)
        return full.strip() if full else None

    def generate_simple(
        self,
        prompt: str,
        max_tokens: int = 300,
        temperature: float = 0.4,
        system: str = "",
        retries: int = 2,
    ) -> Optional[str]:
        """
        Single-shot non-streaming LLM call for agentic sub-tasks
        (planning, verification, summarization).

        Does NOT add to conversation history — each call is stateless.
        Does NOT strip think-tags mid-stream — strips from final result.
        Retries up to ``retries`` times with exponential backoff on failure.
        Returns raw text or None on failure.
        """
        import time as _time

        if not self.enabled or not self.active_model:
            self.active_model = self._resolve_model()
        if not self.active_model:
            return None

        sys_content = system or (
            "You are FRIDAY's internal task engine. "
            "Follow instructions exactly. Be concise and precise."
        )

        payload = {
            "model": self.active_model,
            "messages": [
                {"role": "system", "content": sys_content},
                {"role": "user",   "content": prompt},
            ],
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "num_gpu":    99,
                "num_thread":  8,
                "num_ctx":   4096,
            }
        }
        is_qwen = self.active_model and "qwen" in self.active_model.lower()
        if is_qwen:
            payload["think"] = False

        url  = f"{self.base_url.rstrip('/')}/api/chat"
        body = json.dumps(payload).encode("utf-8")

        last_exc = None
        for attempt in range(1 + retries):
            try:
                req = request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=30.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                raw = data.get("message", {}).get("content", "").strip()
                return self._strip_think_tags(raw) if raw else None
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 1.0 * (2 ** attempt)  # 1s, 2s backoff
                    self._debug(
                        f"generate_simple attempt {attempt+1} failed: {exc} "
                        f"— retrying in {wait:.0f}s"
                    )
                    _time.sleep(wait)

        self._debug(f"generate_simple failed after {1+retries} attempts: {last_exc}")
        return None

    def clear_history(self) -> None:
        self.conversation_history = []
        self._debug("Conversation history cleared.")

    def shutdown(self) -> None:
        if hasattr(self, "screen_oracle") and self.screen_oracle:
            self.screen_oracle.stop()
            self.screen_oracle = None