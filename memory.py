"""
memory.py — Short-term + long-term persistent memory for FRIDAY.

ShortMemory    : in-session rolling string buffer (for memory_hint).
PersistentMemory: saves facts + full conversation history to disk.
                  On init, loads last N turns so Friday remembers
                  previous sessions without any extra infrastructure.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ── Paths ──────────────────────────────────────────────────────────────────

_DEFAULT_DIR   = os.path.join(os.path.expanduser("~"), ".friday")
_MEMORY_FILE   = os.path.join(_DEFAULT_DIR, "memory.json")
_HISTORY_FILE  = os.path.join(_DEFAULT_DIR, "history.jsonl")

# How many past turns to reload into conversation_history at startup
_RELOAD_TURNS  = 6


# ── Short-term memory (rolling string buffer for memory_hint) ──────────────

@dataclass
class ShortMemory:
    """In-session string buffer — passed as memory_hint to the LLM."""
    messages: list[str] = field(default_factory=list)
    max_size: int = 100

    def add(self, message: str) -> None:
        self.messages.append(message)
        if len(self.messages) > self.max_size:
            self.messages = self.messages[-self.max_size:]

    def clear(self) -> None:
        self.messages.clear()

    def recent(self, count: int = 5) -> list[str]:
        return self.messages[-count:]

    def count(self) -> int:
        return len(self.messages)


# ── Persistent memory ──────────────────────────────────────────────────────

@dataclass
class PersistentMemory:
    """
    Long-term memory persisted to ~/.friday/.

    On startup loads the last _RELOAD_TURNS conversation turns from
    history.jsonl so Friday can resume context across sessions.

    get_history_for_llm() returns those turns formatted as
    [{"role": "user", "content": ...}, {"role": "assistant", "content": ...}]
    ready to prepend into brain.conversation_history.
    """
    memory_dir:   str = _DEFAULT_DIR
    memory_file:  str = _MEMORY_FILE
    history_file: str = _HISTORY_FILE

    user_facts:      Dict[str, str]  = field(default_factory=dict)
    preferences:     Dict[str, str]  = field(default_factory=dict)
    session_log:     List[dict]      = field(default_factory=list)
    max_session_log: int             = 200

    # Turns loaded from previous sessions — injected into LLM history
    _loaded_history: List[dict] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        self._load_facts()
        self._load_history()

    # ── Disk I/O ──────────────────────────────────────────────────────────

    def _load_facts(self) -> None:
        """Load persisted facts/preferences from memory.json."""
        if not os.path.exists(self.memory_file):
            return
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.user_facts  = data.get("user_facts",  {})
            self.preferences = data.get("preferences", {})
        except (json.JSONDecodeError, IOError):
            pass

    def _save_facts(self) -> None:
        """Persist facts/preferences to memory.json."""
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump({
                    "user_facts":    self.user_facts,
                    "preferences":   self.preferences,
                    "last_updated":  datetime.now().isoformat(),
                }, f, indent=2, ensure_ascii=False)
        except IOError:
            pass

    def _load_history(self) -> None:
        """
        Read the last _RELOAD_TURNS turns from history.jsonl.
        Skips partial/interrupted entries (assistant starts with '[partial]').
        """
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except IOError:
            return

        turns = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            user_text = entry.get("user", "")
            asst_text = entry.get("assistant", "")
            # Skip skill results and empty turns
            if user_text.startswith("[skill_result]"):
                continue
            if asst_text.startswith("[partial]"):
                asst_text = asst_text.replace("[partial]", "", 1).strip()
            if not user_text or not asst_text:
                continue
            turns.append({"user": user_text, "assistant": asst_text})
            if len(turns) >= _RELOAD_TURNS:
                break

        # Restore chronological order
        self._loaded_history = list(reversed(turns))

    # ── LLM history injection ──────────────────────────────────────────────

    def get_history_for_llm(self) -> List[dict]:
        """
        Return loaded history formatted for brain.conversation_history.
        Call this once at startup and prepend to brain.conversation_history.
        """
        messages = []
        for entry in self._loaded_history:
            messages.append({"role": "user",      "content": entry["user"]})
            messages.append({"role": "assistant",  "content": entry["assistant"]})
        return messages

    # ── Interaction logging ────────────────────────────────────────────────

    def log_interaction(self, user_input: str, assistant_response: str) -> None:
        """Log a turn to session_log (in-memory) and append to history.jsonl."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user":      user_input,
            "assistant": assistant_response,
        }
        self.session_log.append(entry)
        if len(self.session_log) > self.max_session_log:
            self.session_log = self.session_log[-self.max_session_log:]

        try:
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except IOError:
            pass

        # Auto-extract facts from the conversation
        self._auto_extract_facts(user_input, assistant_response)

    # ── Fact auto-extraction ───────────────────────────────────────────────

    # Gerunds / common verbs that start with a capital and cause false name
    # extraction from sentences like "I am Opening Chrome for you".
    _VERB_LOOKAHEAD = (
        r"(?!(?:Opening|Closing|Running|Going|Looking|Searching|Playing|"
        r"Setting|Checking|Taking|Starting|Stopping|Typing|Pressing|"
        r"Clicking|Scrolling|Reading|Writing|Editing|Waiting|Finding|"
        r"Using|Trying|Getting|Doing|Making|Showing|Loading|Sending|"
        r"Working|Coming|Leaving|Moving|Turning|Saving|Copying|"
        r"Downloading|Uploading|Installing|Updating|Launching|"
        r"Focused|Opened|Closed|Done|Sure|Okay|Yes|No|Not|Here|"
        r"Sorry|Right|Just|Now|Also|Already|About|Currently)\b)"
    )

    _FACT_PATTERNS = [
        (r"[mM]y name is " + _VERB_LOOKAHEAD + r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",  "name"),
        (r"[iI](?:'m|\s+am) " + _VERB_LOOKAHEAD + r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", "name"),
        (r"[iI](?:'m|\s+am) (\d+) years? old",                 "age"),
        (r"[iI](?:'m|\s+am) a ([A-Za-z]+ (?:student|engineer|developer|designer|researcher))",
                                                          "profession"),
        (r"[iI]\s+(?:live|am)\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", "location"),
        (r"[iI](?:'m|\s+am)\s+from\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", "location"),
        (r"[iI]\s+(?:like|love|prefer)\s+([A-Za-z]+(?:\s[A-Za-z]+)?)", "preference"),
    ]

    def _auto_extract_facts(self, user_text: str, _asst: str) -> None:
        """Scan user text for self-identifying statements and save as facts."""
        changed = False
        for pattern, key in self._FACT_PATTERNS:
            # Case-sensitive matching preserves the distinction of [A-Z][a-z]+ proper nouns
            m = re.search(pattern, user_text)
            if m:
                value = m.group(1).strip()
                if value and self.user_facts.get(key) != value:
                    self.user_facts[key] = value
                    changed = True
        if changed:
            self._save_facts()

    # ── Fact/preference API ────────────────────────────────────────────────

    def save_fact(self, key: str, value: str) -> None:
        self.user_facts[key.strip().lower()] = value.strip()
        self._save_facts()

    def get_fact(self, key: str) -> Optional[str]:
        return self.user_facts.get(key.strip().lower())

    def save_preference(self, key: str, value: str) -> None:
        self.preferences[key.strip().lower()] = value.strip()
        self._save_facts()

    def get_preference(self, key: str) -> Optional[str]:
        return self.preferences.get(key.strip().lower())

    def semantic_search(self, query: str, limit: int = 3) -> List[dict]:
        """
        Lightweight, pure-Python semantic vector search over the entire history.jsonl.
        Tokenizes text, builds TF-IDF representation, and returns matches using Cosine Similarity.
        """
        if not os.path.exists(self.history_file):
            return []
        
        # 1. Read all turns from history
        turns = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("user") and entry.get("assistant"):
                            turns.append(entry)
                    except Exception:
                        continue
        except Exception:
            return []
            
        if not turns:
            return []
            
        # 2. Tokenize helper
        def tokenize(text: str) -> List[str]:
            text = text.lower()
            text = re.sub(r'[^\w\s]', ' ', text)
            return [w for w in text.split() if len(w) >= 2]
            
        # 3. Build corpus and compute Document Frequency (DF)
        corpus_tokens = []
        df = {}
        for turn in turns:
            tokens = tokenize(turn["user"] + " " + turn["assistant"])
            corpus_tokens.append(tokens)
            unique_tokens = set(tokens)
            for token in unique_tokens:
                df[token] = df.get(token, 0) + 1
                
        # 4. Compute IDF
        import math
        num_docs = len(turns)
        idf = {}
        for token, freq in df.items():
            idf[token] = math.log((1 + num_docs) / (1 + freq)) + 1
            
        # 5. Helper to compute TF-IDF vector
        def get_tfidf_vector(tokens: List[str]) -> Dict[str, float]:
            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            vector = {}
            for t, count in tf.items():
                if t in idf:
                    vector[t] = count * idf[t]
            return vector
            
        # 6. Helper to compute cosine similarity
        def cosine_similarity(v1: Dict[str, float], v2: Dict[str, float]) -> float:
            dot = sum(v1[t] * v2.get(t, 0.0) for t in v1)
            norm1 = math.sqrt(sum(val ** 2 for val in v1.values()))
            norm2 = math.sqrt(sum(val ** 2 for val in v2.values()))
            if norm1 == 0.0 or norm2 == 0.0:
                return 0.0
            return dot / (norm1 * norm2)
            
        # 7. Compute vector for query
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        query_vector = get_tfidf_vector(query_tokens)
        
        # 8. Score each document
        scored_turns = []
        for i, turn in enumerate(turns):
            turn_tokens = corpus_tokens[i]
            if not turn_tokens:
                continue
            turn_vector = get_tfidf_vector(turn_tokens)
            sim = cosine_similarity(query_vector, turn_vector)
            if sim > 0.05:  # small relevance threshold
                scored_turns.append((sim, turn))
                
        # 9. Sort and return top limit
        scored_turns.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_turns[:limit]]

    # ── System prompt context ──────────────────────────────────────────────

    def get_context_summary(self) -> str:
        """
        Build a short context string for the LLM system prompt.
        Includes known facts, preferences, saved notes, and session context.
        """
        parts = []
        if self.user_facts:
            facts = "; ".join(
                f"{k}: {v}" for k, v in list(self.user_facts.items())[:10]
            )
            parts.append(f"Known facts about user: {facts}")
        if self.preferences:
            prefs = "; ".join(
                f"{k}: {v}" for k, v in list(self.preferences.items())[:5]
            )
            parts.append(f"User preferences: {prefs}")

        # Include notes saved via the 'remember' skill
        try:
            if os.path.exists(self.memory_file):
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                notes = data.get("notes", {})
                if notes:
                    note_items = "; ".join(
                        f"{k}: {v}" for k, v in list(notes.items())[:10]
                    )
                    parts.append(f"Saved notes: {note_items}")
        except (json.JSONDecodeError, IOError):
            pass

        if self._loaded_history:
            parts.append(
                f"Note: {len(self._loaded_history)} previous conversation turns "
                f"have been loaded into context from past sessions."
            )
        return ". ".join(parts) if parts else ""

    # ── Cleanup ────────────────────────────────────────────────────────────

    def clear_facts(self) -> None:
        self.user_facts.clear()
        self.preferences.clear()
        self._save_facts()

    def clear_session(self) -> None:
        self.session_log.clear()

    def recent_history(self, count: int = 5) -> List[dict]:
        """Last N turns from this session."""
        return self.session_log[-count:]

    def clear_all_history(self) -> None:
        """Wipe the history file (useful for a fresh start)."""
        try:
            with open(self.history_file, "w") as f:
                pass  # truncate to zero
        except IOError:
            pass
        self._loaded_history = []
        self.session_log = []
