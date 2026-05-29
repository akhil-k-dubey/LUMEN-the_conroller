from __future__ import annotations
import threading
import os
import sys
from typing import Optional

_playwright_module = None
_sync_playwright = None

def _try_import_playwright():
    global _playwright_module, _sync_playwright
    if _sync_playwright is not None:
        return True
    try:
        from playwright.sync_api import sync_playwright
        _sync_playwright = sync_playwright
        return True
    except ImportError:
        return False

class WebAgent:
    _PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".friday", "browser_profile_edge")

    def __init__(self, debug: bool = False):
        self._pw = None
        self._context = None
        self._lock = threading.Lock()
        self._debug = debug
        self._ready = False

    def _log(self, msg: str) -> None:
        if self._debug:
            print(f"[web-agent] {msg}")

    def start(self) -> bool:
        if not _try_import_playwright():
            self._log("Playwright not installed or could not be imported.")
            return False
        try:
            os.makedirs(self._PROFILE_DIR, exist_ok=True)
            self._pw = _sync_playwright().start()

            # Dynamic Browser detection
            # Priority: Edge (where the authenticated session lives) > Brave > default Chromium
            channel = "msedge"
            executable_path = None

            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=self._PROFILE_DIR,
                headless=False,
                channel=channel,
                executable_path=executable_path,
                args=[
                    "--no-default-browser-check", 
                    "--no-first-run",
                    "--disable-blink-features=AutomationControlled"
                ],
                slow_mo=30,
                timeout=20_000,
            )
            self._ready = True
            self._log("Persistent context launched successfully.")
            return True
        except Exception as e:
            self._log(f"Failed to start persistent context (Edge fallback active): {e}")
            # Fallback to Brave / Chrome
            try:
                brave_paths = [
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"BraveSoftware\Brave-Browser\Application\brave.exe")
                ]
                for p in brave_paths:
                    if os.path.exists(p):
                        executable_path = p
                        break
                
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=self._PROFILE_DIR,
                    headless=False,
                    channel=None if executable_path else "chrome",
                    executable_path=executable_path,
                    args=[
                        "--no-default-browser-check", 
                        "--no-first-run",
                        "--disable-blink-features=AutomationControlled"
                    ],
                    slow_mo=30,
                    timeout=20_000,
                )
                self._ready = True
                return True
            except Exception as e2:
                self._log(f"Fallback context launch failed: {e2}")
                if self._pw:
                    try:
                        self._pw.stop()
                    except Exception:
                        pass
                    self._pw = None
                return False

    def stop(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._pw:
                self._pw.stop()
        except Exception as e:
            self._log(f"Error during stop: {e}")
        self._context = None
        self._pw = None
        self._ready = False

    @property
    def available(self) -> bool:
        return self._ready and self._context is not None

    def get_page(self, url_contains: str, navigate_to: str, wait_selector: str = None):
        if not self.available:
            self._log("Get page failed: WebAgent is not available.")
            return None
        
        # Check active pages
        for page in self._context.pages:
            if url_contains in page.url:
                try:
                    page.bring_to_front()
                    return page
                except Exception as e:
                    self._log(f"Failed to bring page to front: {e}")
        
        # Open a new one if not found
        try:
            page = self._context.new_page()
            page.goto(navigate_to, wait_until="domcontentloaded", timeout=20_000)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=15_000)
            return page
        except Exception as e:
            self._log(f"Failed to navigate/open new page: {e}")
            return None

    def whatsapp_send(self, contact: str, message: str) -> str:
        with self._lock:
            try:
                page = self.get_page(
                    "web.whatsapp.com",
                    "https://web.whatsapp.com",
                    wait_selector=None,
                )
                if page is None:
                    return "WebAgent browser context is not available."

                # Wait for WhatsApp Web to load
                try:
                    page.wait_for_selector(
                        'input[placeholder="Search or start new chat"], input[data-tab="3"], div[contenteditable="true"][data-tab="3"], [placeholder="Search or start new chat"], [placeholder="Search"], div.lexical-rich-text-input div[contenteditable="true"], [data-tab="3"]',
                        timeout=15_000,
                    )
                except Exception:
                    # Check if QR code scanner is visible
                    if page.locator('canvas[aria-label="Scan me!"]').count() > 0 or page.locator('canvas').count() > 0:
                        return "WhatsApp Web is not logged in. Please scan the QR code on the browser screen."
                    return "WhatsApp Web timed out loading. Please make sure the browser has an active connection."

                search = page.locator(
                    'input[placeholder="Search or start new chat"], '
                    'input[data-tab="3"], '
                    'div[contenteditable="true"][data-tab="3"], '
                    '[placeholder="Search or start new chat"], '
                    '[placeholder="Search"], '
                    'div.lexical-rich-text-input div[contenteditable="true"]'
                ).first
                search.click()
                page.wait_for_timeout(200)
                
                # Select all and delete to clear previous searches cleanly
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
                
                # Type the contact name
                search.type(contact, delay=60)
                page.wait_for_timeout(1500)  # Wait for search results list to filter

                # Locate the contact item in the list
                contact_result = page.locator(f'span[title="{contact}"]').first
                try:
                    contact_result.wait_for(timeout=6_000)
                except Exception:
                    # Fallback to broader text match if exact title match wasn't found
                    contact_result = page.locator(f'span:has-text("{contact}")').first
                    try:
                        contact_result.wait_for(timeout=4_000)
                    except Exception:
                        return f"Could not find contact '{contact}' in WhatsApp search results."

                contact_result.click()
                page.wait_for_timeout(800)  # Wait for chat pane to open and focus

                # Locate message input box
                msg_box = page.locator(
                    'div[contenteditable="true"][data-tab="10"], '
                    'footer div[contenteditable="true"]'
                ).first
                try:
                    msg_box.wait_for(timeout=5_000)
                except Exception:
                    return "Could not focus the message input box."

                msg_box.click()
                page.wait_for_timeout(200)
                msg_box.fill("")
                page.wait_for_timeout(200)
                msg_box.type(message, delay=40)
                page.wait_for_timeout(200)
                page.keyboard.press("Enter")

                return f"Sent WhatsApp message to {contact}: '{message}'"
            except Exception as exc:
                return f"WhatsApp send failed: {exc}"

    def whatsapp_check(self) -> str:
        with self._lock:
            try:
                page = self.get_page(
                    "web.whatsapp.com",
                    "https://web.whatsapp.com",
                    wait_selector=None,
                )
                if page is None:
                    return "WebAgent browser context is not available."

                # Wait for WhatsApp Web to load
                try:
                    page.wait_for_selector(
                        'input[placeholder="Search or start new chat"], input[data-tab="3"], div[contenteditable="true"][data-tab="3"], [placeholder="Search or start new chat"], [placeholder="Search"], div.lexical-rich-text-input div[contenteditable="true"], [data-tab="3"]',
                        timeout=15_000,
                    )
                except Exception:
                    if page.locator('canvas[aria-label="Scan me!"]').count() > 0 or page.locator('canvas').count() > 0:
                        return "WhatsApp Web is not logged in. Please scan the QR code on the browser screen."
                    return "WhatsApp Web timed out loading."

                # Locate unread badges
                unread = page.locator('[aria-label*="unread"], [data-icon="unread-count"]').all()
                if not unread:
                    return "No unread WhatsApp messages detected."

                names = []
                for badge in unread[:5]:
                    try:
                        # Climb up to get the contact name element
                        name_el = badge.locator('xpath=../../../../..').locator('span[title]').first
                        if name_el.count() > 0:
                            title = name_el.get_attribute("title")
                            if title and title not in names:
                                names.append(title)
                    except Exception:
                        pass
                
                if names:
                    return f"Unread messages from: {', '.join(names)}"
                return f"{len(unread)} unread conversations detected."
            except Exception as exc:
                return f"WhatsApp check failed: {exc}"

    def navigate(self, url: str) -> str:
        with self._lock:
            try:
                if not self.available:
                    return "WebAgent not available."
                pages = self._context.pages
                page = pages[-1] if pages else self._context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                page.bring_to_front()
                return f"Navigated to {url}"
            except Exception as exc:
                return f"Navigation failed: {exc}"

    def read_page_text(self, url_contains: str = None) -> str:
        with self._lock:
            try:
                if not self.available:
                    return "WebAgent not available."
                pages = self._context.pages
                if not pages:
                    return "No pages open."
                page = pages[-1]
                if url_contains:
                    for p in pages:
                        if url_contains in p.url:
                            page = p
                            break
                text = page.evaluate("() => document.body.innerText")
                return text[:3000] if text else "No text found."
            except Exception as exc:
                return f"Read page failed: {exc}"

_web_agent: Optional[WebAgent] = None

def get_web_agent() -> Optional[WebAgent]:
    return _web_agent

def init_web_agent(debug: bool = False) -> bool:
    global _web_agent
    _web_agent = WebAgent(debug=debug)
    return _web_agent.start()
