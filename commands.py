"""
commands.py — Wake word and shutdown command detection.

Upgrades:
  - More natural shutdown phrases
  - More wake word variants
  - Fuzzy matching with better thresholds
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re


def is_shutdown_command(text: str) -> bool:
    """Detect shutdown intent from user input.

    Whisper frequently appends trailing punctuation to short commands
    (e.g. "Shut down." instead of "Shut down"). Strip it before matching
    so these are never silently missed.
    """
    # Strip trailing punctuation that Whisper/STT may append
    normalized = re.sub(r'[.!?,;:]+$', '', text.strip().lower()).strip()
    # If wake up is in the input, avoid shutting down (multi-clause ambiguity)
    if "wake up" in normalized:
        return False
    # Exact matches
    if normalized in {
        "exit", "quit", "shutdown", "shut down",
        "power off", "power down", "goodbye", "good bye",
        "bye friday", "bye bye", "go to sleep",
        "turn off", "end session", "sleep friday",
        # Common voice variants
        "shut down friday", "shutdown friday",
        "goodbye friday", "sleep", "stop friday",
    }:
        return True
    # Partial matches — use clause-boundary detection instead of word count.
    # A clause boundary is: start-of-string, or preceded by punctuation or
    # connecting words (and, but, or, so, then, etc.).
    # This catches "shut down" as a standalone intent even in long input,
    # while ignoring "when does the system shut down for maintenance?".
    shutdown_phrases = {"shutdown", "shut down", "power off", "power down"}
    clause_delimiters = r'(?:^|[.!?,;:]|\b(?:and|but|or|so|then)\b)\s*'
    for phrase in shutdown_phrases:
        # Require phrase at end-of-string (optionally followed by "friday")
        # to prevent false shutdown on sentences like "if the server has to shut down what happens"
        pattern = clause_delimiters + re.escape(phrase) + r'(?:\s+friday)?\s*$'
        if re.search(pattern, normalized, re.IGNORECASE):
            return True
    return False


def is_wake_command(text: str, assistant_name: str) -> bool:
    """Detect wake word from user input.

    Handles Whisper's common mishearings of 'Friday':
      - 'Vehicle friendly' → Friday
      - 'Free day' → Friday
      - 'Freed eye' → Friday
      - 'Fryday' → Friday
    Also accepts bare 'wake up' without a name.
    """
    normalized = text.strip().lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    name = assistant_name.lower()

    # ── Known Whisper misheard aliases for "Friday" ──
    _WHISPER_ALIASES = {
        "vehicle friendly", "vehicle friend", "free day", "freed eye",
        "fryday", "fry day", "fri day", "for a day", "frid ay",
        "fried day", "fried eye", "friday's", "fridays", "friady",
        "fridae", "freeday", "freday", "pridey", "freday",
        "freddy", "fready", "friend", "friendly",
    }

    def name_matches(spoken_name: str) -> bool:
        candidate = spoken_name.strip()
        if not candidate:
            return False
        if candidate == name:
            return True
        # Check known Whisper aliases
        if candidate in _WHISPER_ALIASES:
            return True
        # Fuzzy match — relaxed threshold for voice robustness
        return SequenceMatcher(None, candidate, name).ratio() >= 0.60

    # ── Bare "wake up" (no name) — always treated as wake command ──
    if cleaned in ("wake up", "wakeup", "wake"):
        return True

    # Wake phrases
    prefixes = ["wake up ", "hey ", "hello ", "hi ", "yo ", "okay "]
    suffixes = [" come online", " wake up", " start", " activate"]

    # Check prefixes
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            return name_matches(cleaned[len(prefix):])

    # Check suffixes
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            return name_matches(cleaned[:-len(suffix)])

    # Multi-word alias match (e.g. "vehicle friendly" without prefix)
    if cleaned in _WHISPER_ALIASES:
        return True

    # Just the name alone
    if name_matches(cleaned):
        return True

    return False
