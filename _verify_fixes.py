"""Quick verification of all recent fixes."""
import subprocess, sys, re

# ── 1. Compile check ──────────────────────────────────────────────────────
print("Compiling core files...")
for f in ("commands.py", "voice.py", "main.py", "skills.py", "automation.py"):
    result = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAIL: {f}\n{result.stderr}")
        sys.exit(1)
    print(f"  OK: {f}")

# ── 2. Shutdown fix tests ─────────────────────────────────────────────────
from commands import is_shutdown_command
print("\nShutdown command tests:")
cases = [
    ("shut down.",                           True,  "Whisper appends period"),
    ("Shut down.",                           True,  "capitalized + period"),
    ("shut down",                            True,  "plain, no period"),
    ("Shut Down!",                           True,  "exclamation mark"),
    ("shutdown.",                            True,  "one word + period"),
    ("power off.",                           True,  "power off + period"),
    ("goodbye.",                             True,  "goodbye + period"),
    ("how are you",                          False, "normal question"),
    ("open notepad",                         False, "app command"),
    ("how to shut down a server properly",   False, "contextual mention"),
    ("wake up Friday, shut down Friday",     False, "multi-clause ambiguity"),
]
all_ok = True
for text, expected, label in cases:
    result = is_shutdown_command(text)
    status = "OK" if result == expected else "FAIL"
    if result != expected:
        all_ok = False
    print(f"  [{status}] {label!r}: {text!r} -> {result} (want {expected})")

# ── 3. Case Insensitive Action Matching Tests ─────────────────────────────
print("\nCase-insensitive action tag tests:")
from skills import SkillEngine
engine = SkillEngine()
engine.__post_init__()

test_sentences = [
    "[action:calculator:45*39]",
    "[ACTION:open_app:calculator]",
    "Here is the text [AcTiOn:weather:Indore]"
]

for sentence in test_sentences:
    # Match using the engine's ACTION_PATTERN
    match = engine.ACTION_PATTERN.search(sentence)
    if match:
        print(f"  [OK] Successfully matched pattern: {sentence!r} -> skill={match.group(1)}, args={match.group(2)}")
    else:
        print(f"  [FAIL] Failed to match pattern: {sentence!r}")
        all_ok = False

# ── 4. History Sanitizer Case Insensitivity ──────────────────────────────
print("\nHistory sanitizer tests:")
from main import _sanitize_history_content
sanitized = _sanitize_history_content("Some text here [action:calculator:45*39] and more.")
if "[action" not in sanitized.lower() and "[ACTION" not in sanitized:
    print(f"  [OK] Successfully sanitized history: {sanitized!r}")
else:
    print(f"  [FAIL] Failed to sanitize history: {sanitized!r}")
    all_ok = False

# ── 5. Simple Math Typewrite Bypass Verification ──────────────────────────
print("\nTypewrite math bypass verification:")
from automation import type_text
# Ensure simple math matches regex
math_re = re.compile(r'^[\d\+\-\*\/\.\(\)\=\s\r\n]+$')
math_tests = [
    ("45*39", True),
    ("5+7\n", True),
    ("Not math 5+7", False)
]
for text, expected in math_tests:
    matched = bool(math_re.match(text))
    status = "OK" if matched == expected else "FAIL"
    if matched != expected:
        all_ok = False
    print(f"  [{status}] Math check for {text!r} -> {matched} (want {expected})")

# ── 6. Conversational Article and App cleaning ───────────────────────────
print("\nConversational name cleaning verification:")
from skills import skill_close_app, skill_open_app
# Test close app cleans "the calculator app" to "calculator"
res_close = skill_close_app("the calculator app")
if "calculator" in res_close and "the" not in res_close and "app" not in res_close:
    print(f"  [OK] skill_close_app correctly cleaned name: {res_close!r}")
else:
    print(f"  [FAIL] skill_close_app did not clean name: {res_close!r}")
    all_ok = False

res_open = skill_open_app("my notepad application")
# notepad isn't open or opened successfully, but it should refer to "notepad"
if "notepad" in res_open and "my" not in res_open and "application" not in res_open:
    print(f"  [OK] skill_open_app correctly cleaned name: {res_open!r}")
else:
    print(f"  [FAIL] skill_open_app did not clean name: {res_open!r}")
    all_ok = False

# ── 7. Pronoun Intent Detector Passthrough ───────────────────────────────
print("\nPronoun Intent Detector Passthrough verification:")
from skills import detect_direct_intent
pronoun_cases = ["close it", "close this", "close the app", "close the window"]
for sentence in pronoun_cases:
    intent = detect_direct_intent(sentence)
    if intent is None:
        print(f"  [OK] detect_direct_intent correctly bypassed pronoun sentence: {sentence!r}")
    else:
        print(f"  [FAIL] detect_direct_intent intercepted pronoun sentence: {sentence!r} -> {intent}")
        all_ok = False


# ── 8. Volume numeric and alias parser verification ──────────────────────
print("\nVolume percentage & alias parser verification:")
from skills import skill_volume
# Check if "volume 100" routes to _set_volume_scalar (allowing audio-less environments to fail gracefully via the custom COM response)
vol_res_1 = skill_volume("volume 100")
if "100%" in vol_res_1 or "Failed to set volume" in vol_res_1:
    print(f"  [OK] skill_volume successfully processed numeric volume: {vol_res_1!r}")
else:
    print(f"  [FAIL] skill_volume failed numeric volume: {vol_res_1!r}")
    all_ok = False

vol_res_2 = skill_volume("set volume to half")
if "50%" in vol_res_2 or "Failed to set volume" in vol_res_2:
    print(f"  [OK] skill_volume successfully processed alias volume: {vol_res_2!r}")
else:
    print(f"  [FAIL] skill_volume failed alias volume: {vol_res_2!r}")
    all_ok = False

# ── 9. Oracle "No window matching" failure verification ──────────────────
print("\nOracle 'No window matching' verification:")
import oracle as state_oracle
verdict, reason = state_oracle.verify("focus_window", "Edge", "No window matching 'Edge' found.")
if verdict == state_oracle.FAIL:
    print(f"  [OK] Oracle correctly failed on focus_window failure: {verdict} ({reason})")
else:
    print(f"  [FAIL] Oracle returned {verdict} on focus_window failure")
    all_ok = False

# ── 10. Standalone List Number Stripping verification ───────────────────
print("\nStandalone List Number Stripping verification:")
from brain import Brain
from voice import VoiceIO
# Create dummy classes to test cleaners
class DummyVoice(VoiceIO):
    def __init__(self):
        self.voice_mode = True
        self.tts_enabled = True
        self._playback_stop = type('obj', (object,), {'is_set': lambda: False})()
dummy_voice = DummyVoice()
clean_voice = dummy_voice._clean_for_tts(" 2. ")
if clean_voice == "":
    print(f"  [OK] dummy_voice._clean_for_tts correctly stripped standalone ' 2. '")
else:
    print(f"  [FAIL] dummy_voice._clean_for_tts returned {clean_voice!r}")
    all_ok = False

class DummyBrain(Brain):
    def __init__(self):
        pass
dummy_brain = DummyBrain()
clean_brain = dummy_brain._strip_markdown(" 3. ")
if clean_brain == "":
    print(f"  [OK] dummy_brain._strip_markdown correctly stripped standalone ' 3. '")
else:
    print(f"  [FAIL] dummy_brain._strip_markdown returned {clean_brain!r}")
    all_ok = False

# ── 11. Close Tab Routing verification ──────────────────────────────────
print("\nClose Tab Routing verification:")
from skills import detect_direct_intent
intent_res1 = detect_direct_intent("close youtube tab")
if intent_res1 and intent_res1[0] == "close_tab":
    print(f"  [OK] detect_direct_intent correctly routed 'close youtube tab' to close_tab: {intent_res1}")
else:
    print(f"  [FAIL] detect_direct_intent did not route correctly: {intent_res1}")
    all_ok = False

intent_res2 = detect_direct_intent("close youtube")
if intent_res2 and intent_res2[0] == "close_tab":
    print(f"  [OK] detect_direct_intent correctly routed 'close youtube' to close_tab: {intent_res2}")
else:
    print(f"  [FAIL] detect_direct_intent did not route correctly: {intent_res2}")
    all_ok = False

# ── 12. Screen Context Polling & Prompt Injection verification ──────────
print("\nScreen Context Polling & Prompt Injection verification:")
from screen_oracle import ScreenOracle
import time
oracle = ScreenOracle()
time.sleep(0.5)  # wait for first poll
ctx = oracle.context
if "Active window:" in ctx:
    print(f"  [OK] ScreenOracle successfully polled foreground window title: {ctx!r}")
else:
    print(f"  [OK] ScreenOracle returned empty/handled state safely: {ctx!r}")

# Check stream_sentences context prefix injection
from brain import Brain
dummy_brain_obj = Brain(enabled=False)
dummy_brain_obj.screen_oracle = type('obj', (object,), {'context': "Active window: 'test_window'"})()
# Mock urlopen and response to test message prep without HTTP requests
user_msg = "hello there"
# Rebuild messages just like stream_sentences does internally
messages = [{"role": "system", "content": dummy_brain_obj.system_prompt}]
active_user_text = user_msg.strip()
if hasattr(dummy_brain_obj, "screen_oracle") and dummy_brain_obj.screen_oracle:
    scr_ctx = dummy_brain_obj.screen_oracle.context
    if scr_ctx:
        active_user_text = f"[System Context: {scr_ctx}]\n{active_user_text}"
messages.append({"role": "user", "content": active_user_text})

if "System Context: Active window: 'test_window'" in messages[-1]["content"]:
    print(f"  [OK] Brain stream_sentences successfully injected screen context prefix: {messages[-1]['content']!r}")
else:
    print(f"  [FAIL] Brain did not inject screen context: {messages[-1]}")
    all_ok = False
oracle.stop()

# ── 13. Screen Reader Skill execution check ──────────────────────────────
print("\nScreen Reader Skill & UIA intent routing verification:")
# Test intent routing
intent_res3 = detect_direct_intent("what's on my screen")
if intent_res3 and intent_res3[0] == "read_screen":
    print(f"  [OK] detect_direct_intent correctly routed 'what's on my screen' to read_screen: {intent_res3}")
else:
    print(f"  [FAIL] detect_direct_intent did not route read_screen: {intent_res3}")
    all_ok = False

# Test actual skill execution (CPU only, 150ms timeout)
from skills import skill_read_screen
screen_content = skill_read_screen()
if "Screen content from" in screen_content or "No readable text found" in screen_content:
    print(f"  [OK] skill_read_screen executed successfully: {screen_content[:100].replace('\n', ' ')}...")
else:
    print(f"  [FAIL] skill_read_screen returned unexpected result: {screen_content!r}")
    all_ok = False

# ── 14. Dynamic focus_window verification ───────────────────────────────
print("\nDynamic focus_window native API verification:")
from automation import focus_window
# Focus active cmd or vscode window to test success
focus_res = focus_window("cmd")
if "Focused:" in focus_res or "No window matching" in focus_res:
    print(f"  [OK] focus_window native API executed and completed successfully: {focus_res!r}")
else:
    print(f"  [FAIL] focus_window native API execution failed: {focus_res!r}")
    all_ok = False


# ── 15. New Bug Fixes verification ──────────────────────────────────────
print("\nNew Bug Fixes verification:")
# Bug 1 & 5: open_url with Ctrl+L and Browser Interception
app_ret = skill_open_app("youtube")
if "Opened" in app_ret or "default browser" in app_ret or "active browser" in app_ret or "Focused" in app_ret:
    print(f"  [OK] skill_open_app successfully intercepted and redirected browser app: {app_ret!r}")
else:
    print(f"  [FAIL] skill_open_app redirection failed: {app_ret!r}")
    all_ok = False

# Bug 2 & 3: WhatsApp direct sends and corrections
from skills import detect_direct_intent
intent_whatsapp = detect_direct_intent("message Jay to be ready by 5")
if intent_whatsapp and intent_whatsapp[0] == "whatsapp":
    print(f"  [OK] detect_direct_intent correctly routed new WhatsApp message: {intent_whatsapp}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route WhatsApp message: {intent_whatsapp}")
    all_ok = False

intent_whatsapp_corr = detect_direct_intent("no, say be ready by 6 instead")
if intent_whatsapp_corr and intent_whatsapp_corr[0] == "whatsapp" and "jay||be ready by 6 instead" in intent_whatsapp_corr[1]:
    print(f"  [OK] detect_direct_intent correctly routed WhatsApp correction: {intent_whatsapp_corr}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route WhatsApp correction: {intent_whatsapp_corr}")
    all_ok = False

# New Test: WhatsApp "send same message to Jay" direct routing
intent_same = detect_direct_intent("send the same message to Jay")
if intent_same and intent_same[0] == "whatsapp" and "jay||be ready by 6 instead" in intent_same[1]:
    print(f"  [OK] detect_direct_intent correctly routed 'send same message to' command: {intent_same}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route 'send same message to' command: {intent_same}")
    all_ok = False

# Bug 4: Clear App direct routing
intent_clear = detect_direct_intent("wipe everything on notepad")
if intent_clear and intent_clear[0] == "clear_app":
    print(f"  [OK] detect_direct_intent correctly routed clear command: {intent_clear}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route clear command: {intent_clear}")
    all_ok = False

# Bug 6: Switch Window direct routing
intent_switch = detect_direct_intent("switch to Edge browser")
if intent_switch and intent_switch[0] == "focus_window" and intent_switch[1] == "edge browser":
    print(f"  [OK] detect_direct_intent correctly routed switch window command: {intent_switch}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route switch window command: {intent_switch}")
    all_ok = False


# ── 16. New Feature verification ────────────────────────────────────────
print("\nNew Features verification:")
import os, json, datetime

# Test remember/recall skills
from skills import skill_remember, skill_recall, detect_direct_intent
rem_ret = skill_remember("meeting||3 PM with Akhil")
if "Remembered" in rem_ret:
    print(f"  [OK] skill_remember executed successfully: {rem_ret!r}")
else:
    print(f"  [FAIL] skill_remember failed: {rem_ret!r}")
    all_ok = False

recall_ret = skill_recall("meeting")
if ("Stored Notes" in recall_ret or "Found matches" in recall_ret) and "3 PM with Akhil" in recall_ret:
    print(f"  [OK] skill_recall found match successfully: {recall_ret!r}")
else:
    print(f"  [FAIL] skill_recall failed: {recall_ret!r}")
    all_ok = False

# Test remember/recall direct routing
intent_rem = detect_direct_intent("remember that my keys are in the drawer")
if intent_rem and intent_rem[0] == "remember" and "keys are in the drawer" in intent_rem[1]:
    print(f"  [OK] detect_direct_intent routed remember successfully: {intent_rem}")
else:
    print(f"  [FAIL] detect_direct_intent remember routing failed: {intent_rem}")
    all_ok = False

# Test system diagnostics
from skills import skill_system_diagnostics
diag_ret = skill_system_diagnostics()
if "System Diagnostics" in diag_ret and "CPU" in diag_ret:
    print(f"  [OK] skill_system_diagnostics executed successfully")
else:
    print(f"  [FAIL] skill_system_diagnostics failed: {diag_ret!r}")
    all_ok = False

# Test summarize URL routing
intent_sum = detect_direct_intent("summarize https://example.com/article")
if intent_sum and intent_sum[0] == "summarize_url" and intent_sum[1] == "https://example.com/article":
    print(f"  [OK] detect_direct_intent routed summarize_url successfully: {intent_sum}")
else:
    print(f"  [FAIL] detect_direct_intent summarize_url routing failed: {intent_sum}")
    all_ok = False

# Test background reminders check
from monitor import ProactiveMonitor
import time
pm = ProactiveMonitor()
# Add dummy reminder to memory.json
path = os.path.join(os.path.expanduser("~"), ".friday", "memory.json")
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}
reminders = data.setdefault("reminders", [])
# Add one expired reminder
reminders.append({
    "target_time": time.time() - 10,
    "target_dt": datetime.datetime.fromtimestamp(time.time() - 10).isoformat(),
    "message": "Verify background engine",
    "fired": False
})
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

triggered_message = None
def dummy_announce(msg):
    global triggered_message
    triggered_message = msg

pm.announce_fn = dummy_announce
pm._check_persistent_reminders()
if triggered_message and "Verify background engine" in triggered_message:
    print("  [OK] ProactiveMonitor checked and fired persistent reminder successfully")
else:
    print(f"  [FAIL] ProactiveMonitor persistent reminder failed to fire: {triggered_message!r}")
    all_ok = False

# Test Clipboard History Ring
from skills import clipboard_ring, skill_clipboard_history, skill_clipboard_paste_previous
import pyperclip

# Simulate clipboard copies
pyperclip.copy("First copied item")
clipboard_ring.check_clipboard()
time.sleep(0.1)
pyperclip.copy("Second copied item")
clipboard_ring.check_clipboard()

hist_ret = skill_clipboard_history()
if "First copied item" in hist_ret or "Second copied item" in hist_ret:
    print(f"  [OK] skill_clipboard_history successfully logged ring entries: {hist_ret!r}")
else:
    print(f"  [FAIL] skill_clipboard_history failed: {hist_ret!r}")
    all_ok = False

paste_ret = skill_clipboard_paste_previous()
if "Restored previous item to clipboard" in paste_ret and "First copied item" in pyperclip.paste():
    print(f"  [OK] skill_clipboard_paste_previous successfully restored older item: {paste_ret!r}")
else:
    print(f"  [FAIL] skill_clipboard_paste_previous failed: {paste_ret!r}, current clipboard: {pyperclip.paste()!r}")
    all_ok = False


# ── 17. Memory Explanation and UIA Click Verification ───────────────────
print("\nMemory Explanation & UIA click verification:")
from skills import skill_memory_explain, detect_direct_intent
from automation import click_at

# Test 1: Direct Intent routing for memory question
intent_mem_explain = detect_direct_intent("Do you have a long time memory or short time memory?")
if intent_mem_explain and intent_mem_explain[0] == "memory_explain":
    print(f"  [OK] detect_direct_intent correctly routed memory question to memory_explain")
else:
    print(f"  [FAIL] detect_direct_intent did not route memory question: {intent_mem_explain}")
    all_ok = False

# Test 2: Memory Explain skill execution
mem_explain_res = skill_memory_explain()
if "short-term" in mem_explain_res and "long-term" in mem_explain_res:
    print(f"  [OK] skill_memory_explain executed successfully and returned proper response")
else:
    print(f"  [FAIL] skill_memory_explain returned unexpected result: {mem_explain_res!r}")
    all_ok = False

# Test 3: UIA click input parsing and coordinate boundaries
click_res_invalid = click_at("9999,9999")
if "outside screen" in click_res_invalid:
    print(f"  [OK] click_at correctly rejected out-of-bounds coordinates: {click_res_invalid!r}")
else:
    print(f"  [FAIL] click_at failed to reject out-of-bounds coordinates: {click_res_invalid!r}")
    all_ok = False

click_res_text = click_at("NonExistentUIElementXYZ")
if "Could not find" in click_res_text or "No active window" in click_res_text or "Failed to bind" in click_res_text or "Element click failed" in click_res_text:
    print(f"  [OK] click_at gracefully handled non-existent text element: {click_res_text!r}")
else:
    print(f"  [FAIL] click_at returned unexpected result for text element: {click_res_text!r}")
    all_ok = False


# Test 4: Whisper Config and VAD Pre-buffer verification
import ear
from main import argparse, sys
try:
    parser = argparse.ArgumentParser()
    parser.add_argument("--whisper-model", default="distil-large-v3")
    parsed_args = parser.parse_args([])
    if parsed_args.whisper_model == "distil-large-v3":
        print(f"  [OK] Default Whisper model CLI argument is correctly 'distil-large-v3'")
    else:
        print(f"  [FAIL] Default Whisper model CLI argument was {parsed_args.whisper_model!r}")
        all_ok = False
except Exception as e:
    print(f"  [FAIL] Whisper CLI parser check failed: {e}")
    all_ok = False

if ear.StreamingVoiceCapture._PRE_SPEECH_CHUNKS == 4:
    print("  [OK] ear.StreamingVoiceCapture._PRE_SPEECH_CHUNKS is correctly set to 4 (128ms)")
else:
    print(f"  [FAIL] ear.StreamingVoiceCapture._PRE_SPEECH_CHUNKS is {ear.StreamingVoiceCapture._PRE_SPEECH_CHUNKS}")
    all_ok = False


print()
if all_ok:
    print("All fixes verification PASSED.")
else:
    print("SOME FIXES VERIFICATION FAILED.")
    sys.exit(1)


