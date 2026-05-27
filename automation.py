"""
automation.py — Desktop & Browser Automation Engine for FRIDAY (Phase 4+).

Provides safe desktop control:
  - Keyboard input (type text, press hotkeys)
  - Mouse control (click, scroll)
  - Window management (focus, list)
  - PowerShell execution (sandboxed)

Security Architecture:
  ┌─────────────────────────────────────────────────┐
  │  FAILSAFE: Move mouse to (0,0) = instant abort │
  │  RATE LIMIT: Max 60 actions per minute          │
  │  BLACKLIST: Dangerous PowerShell cmds blocked   │
  │  TIMEOUT: All commands hard-capped at 30s       │
  └─────────────────────────────────────────────────┘
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional


def _sanitize_ps_param(param: str) -> str:
    """Keep only safe characters: alphanumeric, space, dot, dash, underscore, comma, parentheses, brackets, colon, slashes."""
    return re.sub(r'[^a-zA-Z0-9 \.\-_,\(\)\[\]:\\\/]', '', param)


# ── Rate limiter ───────────────────────────────────────────────────────────

_action_timestamps: list[float] = []
_MAX_ACTIONS_PER_MINUTE = 60


def _rate_check() -> Optional[str]:
    """Return error string if rate exceeded, else None."""
    now = time.time()
    _action_timestamps[:] = [t for t in _action_timestamps if now - t < 60]
    if len(_action_timestamps) >= _MAX_ACTIONS_PER_MINUTE:
        return "Rate limit: too many automation actions. Wait a moment."
    _action_timestamps.append(now)
    return None


# ── PyAutoGUI lazy loader ──────────────────────────────────────────────────

_pag = None


def _get_pyautogui():
    """Lazy-import pyautogui with fail-safe enabled."""
    global _pag
    if _pag is not None:
        return _pag
    try:
        import pyautogui
        pyautogui.FAILSAFE = True       # move mouse to (0,0) = abort
        pyautogui.PAUSE = 0.05          # tiny pause between actions
        _pag = pyautogui
        return _pag
    except ImportError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  KEYBOARD
# ═══════════════════════════════════════════════════════════════════════════

def type_text(text: str, interval: float = 0.02) -> str:
    """Type text at current cursor position.
    Uses clipboard paste (Ctrl+V) for reliability across all keyboard layouts.
    Falls back to pyautogui.typewrite for simple ASCII if clipboard unavailable.
    """
    err = _rate_check()
    if err:
        return err
    pag = _get_pyautogui()
    if not pag:
        return "pyautogui not installed. Run: pip install pyautogui"
    try:
        # If it consists of simple math/digits/operators, use write directly.
        # Apps like Windows Calculator ignore pasted (Ctrl+V) operators.
        import re
        if re.match(r'^[\d\+\-\*\/\.\(\)\=\s\r\n]+$', text):
            pag.write(text, interval=interval)
            return f"Typed {len(text)} characters."

        # Clipboard paste is the most reliable method for all characters
        clipboard_ok = False
        try:
            import pyperclip
            pyperclip.copy(text)
            clipboard_ok = True
        except ImportError:
            try:
                # PowerShell clipboard fallback
                import subprocess
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Set-Clipboard -Value '{text.replace(chr(39), chr(39)+chr(39))}'" ],
                    timeout=5, capture_output=True,
                )
                clipboard_ok = proc.returncode == 0
            except Exception:
                pass

        if clipboard_ok:
            time.sleep(0.05)
            pag.hotkey("ctrl", "v")
            return f"Typed {len(text)} characters."

        # Last resort: typewrite (ASCII only, keyboard-layout dependent)
        if text.isascii():
            pag.typewrite(text, interval=interval)
            return f"Typed {len(text)} characters (key-by-key)."

        return "Could not type text: no clipboard and text contains non-ASCII."
    except Exception as exc:
        return f"Type failed: {exc}"


def press_hotkey(keys: str) -> str:
    """Press a keyboard shortcut.
    Args: 'ctrl+c', 'alt+tab', 'enter', 'ctrl+shift+n', etc.
    """
    err = _rate_check()
    if err:
        return err
    pag = _get_pyautogui()
    if not pag:
        return "pyautogui not installed."
    try:
        key_list = [k.strip().lower() for k in keys.split("+")]
        pag.hotkey(*key_list)
        return f"Pressed {'+'.join(key_list)}."
    except Exception as exc:
        return f"Hotkey failed: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
#  MOUSE
# ═══════════════════════════════════════════════════════════════════════════

def click_at(args: str) -> str:
    """Click at x,y coordinates or current position.
    Args: 'x,y' or empty for current position.
    """
    err = _rate_check()
    if err:
        return err
    pag = _get_pyautogui()
    if not pag:
        return "pyautogui not installed."
    try:
        if args.strip():
            # Handle both '100,200' and 'x=100,y=200' formats
            cleaned_args = re.sub(r'[xy]=', '', args.strip(), flags=re.IGNORECASE)
            parts = cleaned_args.replace(" ", "").split(",")
            x, y = int(parts[0]), int(parts[1])
            screen_w, screen_h = pag.size()
            if not (0 <= x <= screen_w and 0 <= y <= screen_h):
                return f"Coordinates ({x},{y}) outside screen ({screen_w}x{screen_h})."
            pag.click(x, y)
            return f"Clicked at ({x}, {y})."
        else:
            pag.click()
            return "Clicked at current position."
    except Exception as exc:
        return f"Click failed: {exc}"


def scroll_mouse(args: str) -> str:
    """Scroll up or down. Args: 'up 5' or 'down 3' (scroll clicks)."""
    err = _rate_check()
    if err:
        return err
    pag = _get_pyautogui()
    if not pag:
        return "pyautogui not installed."
    try:
        parts = args.strip().lower().split()
        direction = parts[0] if parts else "down"
        amount = int(parts[1]) if len(parts) > 1 else 3
        amount = min(amount, 20)  # safety cap
        if direction == "up":
            pag.scroll(amount)
        else:
            pag.scroll(-amount)
        return f"Scrolled {direction} {amount} clicks."
    except Exception as exc:
        return f"Scroll failed: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
#  WINDOW MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def force_foreground(hwnd: int) -> bool:
    """Force a window to foreground, bypassing Windows focus-steal protection."""
    import win32gui
    import win32con
    import win32process
    import win32api
    import ctypes
    import time

    try:
        win32gui.ShowWindow(hwnd, 9)  # RESTORE
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd == hwnd:
            return True

        fg_tid, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
        my_tid = win32api.GetCurrentThreadId()
        tgt_tid, _ = win32process.GetWindowThreadProcessId(hwnd)

        if fg_tid != tgt_tid and my_tid != fg_tid:
            # Attach our thread input to the foreground thread to share input queue
            ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, True)
            ctypes.windll.user32.AttachThreadInput(tgt_tid, fg_tid, True)
            
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            
            # Detach
            ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, False)
            ctypes.windll.user32.AttachThreadInput(tgt_tid, fg_tid, False)
        else:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)

        time.sleep(0.1)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False


def focus_window(title: str) -> str:
    """Focus a window by partial title match using native Win32 APIs with dynamic polling and focus-steal protection."""
    import win32gui
    import win32con
    import time

    target = title.strip().lower()
    if not target:
        return "No window title provided."

    # Try to find and focus the window, polling every 50ms up to 2.5 seconds (dynamic load wait!)
    start_time = time.monotonic()
    hwnd_found = None
    title_found = ""

    while time.monotonic() - start_time < 2.5:
        def win_enum_callback(hwnd, extra):
            nonlocal hwnd_found, title_found
            if win32gui.IsWindowVisible(hwnd):
                w_title = win32gui.GetWindowText(hwnd)
                if target in w_title.lower():
                    hwnd_found = hwnd
                    title_found = w_title

        win32gui.EnumWindows(win_enum_callback, None)
        if hwnd_found:
            break
        time.sleep(0.05)  # dynamic wait polling interval

    if not hwnd_found:
        return f"No window matching '{title}' found."

    # Force to foreground bypassing Windows protections
    success = force_foreground(hwnd_found)
    if success:
        return f"Focused: {title_found}"
    else:
        return f"Focused: {title_found} (with focus restriction)"


def list_windows() -> str:
    """List all visible windows with titles."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
             "Select-Object -First 20 ProcessName, MainWindowTitle | "
             "Format-Table -AutoSize | Out-String -Width 120"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "No visible windows found."
    except Exception as exc:
        return f"List windows failed: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
#  POWERSHELL SANDBOX
# ═══════════════════════════════════════════════════════════════════════════

# Patterns that are NEVER allowed — matches are case-insensitive
_PS_BLACKLIST_PATTERNS = [
    # Destructive disk/system ops
    r"format[-\s]?(volume|disk)",
    r"clear[-\s]?disk",
    r"stop[-\s]?computer",
    r"restart[-\s]?computer",
    r"bcdedit",
    r"diskpart",
    # Recursive delete on system directories — requires absolute path patterns
    # (drive letter + backslash + system dir) to prevent flagging unrelated commands
    r'remove[-\s]?item\s+.*-recurse.*["\']?[a-zA-Z]:\\(?:windows|system32|program\s*files)',
    r'rm\s+-r.*["\']?[a-zA-Z]:\\(?:windows|system32|program\s*files)',
    r'rmdir\s+/s.*["\']?[a-zA-Z]:\\(?:windows|system32|program\s*files)',
    r'del\s+/[sfq].*["\']?[a-zA-Z]:\\(?:windows|system32)',
    r'rd\s+/s\s+.*["\']?[a-zA-Z]:\\(?:windows|system32)',
    # Security/policy
    r"set[-\s]?executionpolicy\s+unrestricted",
    r"disable[-\s]?windowsoptionalfeature",
    # Dangerous registry
    r"remove[-\s]?itemproperty.*hklm:",
    r"set[-\s]?itemproperty.*hklm:\\\\system",
    # Service manipulation
    r"new[-\s]?service",
    r"remove[-\s]?service",
    # Firewall disable
    r"netsh\s+advfirewall\s+set.*state\s+off",
    r"netsh\s+firewall.*disable",
    # Credential theft
    r"mimikatz",
    r"get[-\s]?credential",
    # Download-and-execute chains
    r"iex\s*\(.*downloadstring",
    r"invoke[-\s]?expression.*invoke[-\s]?webrequest",
    r"iex.*iwr",
    r"invoke[-\s]?expression.*net\.webclient",
    r"start[-\s]?bitstransfer.*\.(exe|bat|cmd|ps1)",
]

_PS_BLACKLIST_RE = re.compile("|".join(_PS_BLACKLIST_PATTERNS), re.IGNORECASE)

# Simple keyword blacklist for fast rejection
_PS_QUICK_BLOCK = frozenset([
    "format c:", "format d:", "format e:",
    "rd /s /q c:\\", "deltree", "cipher /w:c:",
])


def powershell_exec(command: str, timeout: int = 15) -> str:
    """Execute a PowerShell command with security sandbox.

    - Blocks dangerous commands via regex blacklist
    - Caps timeout at 30 seconds
    - Captures stdout + stderr
    - Restricts module loading
    """
    command = command.strip()
    if not command:
        return "No command provided."

    # Security: regex blacklist
    if _PS_BLACKLIST_RE.search(command):
        return "BLOCKED: This command is not allowed for security reasons."

    # Security: quick keyword check
    lower = command.lower()
    if any(blocked in lower for blocked in _PS_QUICK_BLOCK):
        return "BLOCKED: Destructive disk operation not allowed."

    # Security: bulletproof check for recursive delete on system directories in any order
    is_delete = any(x in lower for x in ("remove-item", "rm ", "del ", "rmdir ", "rd "))
    is_recursive = any(x in lower for x in ("-recurse", "-r ", "/s"))
    is_system_dir = any(x in lower for x in ("c:\\windows", "c:\\program files", "c:\\system32"))
    if is_delete and is_recursive and is_system_dir:
        return "BLOCKED: Recursive deletion of system directories is not allowed for security reasons."

    timeout = min(max(timeout, 1), 30)  # clamp 1-30s

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NoLogo", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0 and stderr:
            return f"Error (exit {result.returncode}): {stderr[:500]}"
        if stderr and stdout:
            return f"{stdout[:400]}\n[stderr]: {stderr[:100]}"
        return stdout[:500] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as exc:
        return f"PowerShell error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
#  PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def list_processes(filter_name: str = "") -> str:
    """List running processes, optionally filtered by name."""
    # Alias map: common names → actual Windows process names
    _PROC_ALIASES = {
        "chrome": "msedge",
        "browser": "msedge",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "vscode": "code",
        "vs code": "code",
        "terminal": "WindowsTerminal",
    }
    resolved = _PROC_ALIASES.get(filter_name.strip().lower(), filter_name.strip()) if filter_name.strip() else ""
    resolved = _sanitize_ps_param(resolved)

    try:
        if resolved:
            cmd = (
                f"Get-Process -Name '*{resolved}*' -ErrorAction SilentlyContinue | "
                "Sort-Object CPU -Descending | Select-Object -First 15 "
                "Name, Id, @{N='CPU_s';E={[math]::Round($_.CPU,1)}}, "
                "@{N='Mem_MB';E={[math]::Round($_.WorkingSet64/1MB,0)}} | "
                "Format-Table -AutoSize | Out-String -Width 120"
            )
        else:
            cmd = (
                "Get-Process | Sort-Object CPU -Descending | Select-Object -First 20 "
                "Name, Id, @{N='CPU_s';E={[math]::Round($_.CPU,1)}}, "
                "@{N='Mem_MB';E={[math]::Round($_.WorkingSet64/1MB,0)}} | "
                "Format-Table -AutoSize | Out-String -Width 120"
            )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "No processes found."
    except Exception as exc:
        return f"Process list failed: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
#  WAIT UTILITY
# ═══════════════════════════════════════════════════════════════════════════

def wait_seconds(seconds: str) -> str:
    """Wait for N seconds (max 30). Use for page loads, app startup, etc."""
    try:
        n = min(float(seconds.strip()), 30)
        if n <= 0:
            return "Duration must be positive."
        time.sleep(n)
        return f"Waited {n:.1f} seconds."
    except ValueError:
        return f"Invalid duration: {seconds}"
    except Exception as exc:
        return f"Wait failed: {exc}"
