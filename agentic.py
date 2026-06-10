"""
agentic.py — FRIDAY Agentic Task Loop (Phase 4)

Architecture:
  User task (multi-step request)
      → Plan   : LLM breaks task into ordered steps
      → Execute : run each step's skill/code action
      → Verify  : check output, decide retry / next step / done
      → Done   : speak concise summary

Safety:
  - Max 15 steps per task (hard ceiling)
  - Confirmation gate: destructive ops (file_write, run_code with mutations)
    ask the user before executing
  - Results truncated to 500 chars before being spoken
  - All skill errors are caught and reported cleanly — never crash the loop
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any

from skills import SkillEngine
import oracle as state_oracle


# ── Step status constants ──────────────────────────────────────────────────
STEP_OK      = "ok"
STEP_RETRY   = "retry"
STEP_FAIL    = "fail"
STEP_SKIP    = "skip"
STEP_CONFIRM = "needs_confirm"
STEP_PENDING = "pending"

_DESTRUCTIVE_SKILLS = {"file_write", "file_edit", "run_code", "close_app", "powershell", "whatsapp", "clear_app", "rename_file", "move_file"}

# ── Skills that immediately abort the task chain if they fail ─────────────
_ABORT_CHAIN_ON_FAIL = {"focus_window", "open_app", "find_and_open", "smart_open", "open_url"}

# Max retries per step before marking it failed
_MAX_STEP_RETRIES = 2

# Max code self-fix attempts (run → error → LLM fix → re-run)
_CODE_FIX_MAX = 3

# Error indicators that trigger the self-fix loop
_CODE_ERROR_INDICATORS = [
    "Error:", "Traceback", "SyntaxError", "NameError", "TypeError",
    "ValueError", "IndentationError", "ModuleNotFoundError",
    "ImportError", "AttributeError", "KeyError", "IndexError",
    "FileNotFoundError", "ZeroDivisionError", "RuntimeError",
]

# Max total steps per agentic task
MAX_STEPS = 15

# Result truncation for voice
_RESULT_VOICE_LIMIT = 500


def _truncate_voice(text: str) -> str:
    """Trim result to voice-safe length."""
    text = text.strip()
    if len(text) > _RESULT_VOICE_LIMIT:
        return text[:_RESULT_VOICE_LIMIT] + "… (truncated)"
    return text


@dataclass
class AgentStep:
    """One planned step in the agentic task."""
    index:       int
    description: str         # human-readable description
    skill:       str         # skill name to call
    args:        str         # arguments for the skill
    result:      str  = ""
    status:      str  = STEP_PENDING
    retries:     int  = 0
    hint:        str  = ""


@dataclass
class AgenticTaskLoop:
    """
    Agentic execution loop for multi-step tasks.

    Usage:
        loop = AgenticTaskLoop(
            brain=brain,
            skill_engine=skill_engine,
            speak_fn=voice.speak,
            confirm_fn=get_user_input,    # callable that returns user text
            debug=True,
        )
        summary = loop.run("create a python hello world file and run it")

    The loop:
        1. PLAN   — sends user task to LLM asking for a JSON step list
        2. EXECUTE — for each step, calls the corresponding skill
        3. VERIFY  — feeds result back to LLM: ok / retry / next / done?
        4. DONE   — collects all results, asks LLM for a short summary
    """

    brain:       Any                      # Brain instance
    skill_engine: SkillEngine
    speak_fn:    Callable[[str], None]    # voice.speak
    print_fn:    Callable[[str], None] = print
    confirm_fn:  Optional[Callable[[str], Optional[str]]] = None  # prompts user
    debug:       bool = False

    def _log(self, msg: str) -> None:
        if self.debug:
            self.print_fn(f"[agentic] {msg}")

    # ── Phase 1: PLAN ─────────────────────────────────────────────────────

    _PLAN_RETRIES = 1  # retry once on LLM failure before giving up

    def _plan(self, task: str) -> tuple[List[AgentStep], bool]:
        """
        Ask the LLM to decompose the task into ordered steps.
        Returns a tuple of (List[AgentStep], plan_failed: bool).
        Retries up to _PLAN_RETRIES times if the LLM is unreachable or
        returns unparseable output. Returns plan_failed=True so the caller
        can distinguish Q&A (empty plan) from Ollama failure.
        """
        import os
        import getpass
        try:
            username = getpass.getuser()
        except Exception:
            username = "User"
        home_dir = os.path.expanduser("~")

        plan_prompt = (
            f"You are FRIDAY's task planner. Break this task into steps:\n"
            f"TASK: {task}\n\n"
            f"Available skills (use ONLY these exact names in the 'skill' field):\n"
            f"  {', '.join(sorted(self.skill_engine.skills.keys()))}\n\n"
            f"Reply ONLY with a JSON array of steps. Each step:\n"
            f'  {{"step": 1, "description": "what this step does", '
            f'"skill": "skill_name", "args": "skill arguments"}}\n\n'
            f"Rules:\n"
            f"- Max {MAX_STEPS} steps\n"
            f"- The 'skill' field must be one of the exact names listed above\n"
            f"- For open_app: args = app name (e.g. notepad, cmd, powershell, msedge)\n"
            f"- For close_app: args = process name — KILLS the ENTIRE application\n"
            f"- For close_tab: args = window hint (e.g. YouTube) — closes ONE browser tab with Ctrl+W\n"
            f"- IMPORTANT: To close a browser TAB, use close_tab, NOT close_app\n"
            f"- IMPORTANT: close_app kills the ENTIRE browser — only use when user wants the whole app closed\n"
            f"- For smart_open: args = site name or URL (e.g. youtube, google, https://...)\n"
            f"- For youtube_search: args = search query. Use this direct skill to search and directly play videos/music/songs on YouTube in one step. Do NOT open the browser manually or type text when the user asks to play a YouTube video or song.\n"
            f"- For web_search: args = search query. Use this to search the web for general facts, summaries, or info using DuckDuckGo.\n"
            f"- For browser_search: args = search query. Use this to search the web directly inside a new browser tab.\n"
            f"- For file_write: args = path||content\n"
            f"- For file_edit: args = path||old_text||new_text (surgical replace)\n"
            f"- For run_code: args = python:code_here\n"
            f"- For file_read: args = path\n"
            f"- For git_query: args = status | diff | log | branch\n"
            f"- For type_text: args = text to type\n"
            f"- For press_keys: args = ctrl+c or alt+tab etc.\n"
            f"- For click: args = 500,300 (x,y coordinates)\n"
            f"- For focus_window: args = partial window title (e.g. Notepad, Edge)\n"
            f"- For wait: args = seconds (use ONLY when explicit delay is requested by the user. Do NOT add wait steps after open_app/smart_open because window loading is now handled dynamically!)\n"
            f"- For powershell: args = PowerShell command\n"
            f"- For calculator: args = math expression (e.g. 5 * 7). Use this direct skill ONLY for quick, direct math questions without opening any apps. Do NOT use the direct 'calculator' skill if the user explicitly asks to open, run, or use the Windows calculator app (calc.exe) / GUI.\n"
            f"- If you must interact with the Windows calculator GUI, do NOT use the direct 'calculator' skill. Instead, the sequence must be: 1. open_app calculator -> 2. focus_window Calculator -> 3. type_text first_number -> 4. type_text operator (use '*' for multiplication, never 'ast' or 'times') -> 5. type_text second_number -> 6. press_keys enter.\n"
            f"- For whatsapp: args = contact||message. ALWAYS use this direct skill to search, select, and send a message to a contact on WhatsApp. Do NOT try to manually open Edge, search contacts, or click inside WhatsApp Web via type_text/press_keys/click. The whatsapp skill handles everything automatically.\n"
            f"- For whatsapp_check_messages: args = empty. Use this direct skill to check for new or unread WhatsApp messages.\n"
            f"- To open a terminal: use open_app with args=cmd or args=powershell\n"
            f"- The default browser is Microsoft Edge (not Chrome)\n"
            f"- Use only ONE skill per step\n"
            f"- Call open_app only ONCE per application — never open the same app multiple times\n"
            f"- Do NOT add close/cleanup steps unless the user explicitly asked to close something\n"
            f"- If the task needs no skills (pure Q&A), return an empty array []\n"
            f"- To navigate a browser to a URL, ALWAYS use open_url <url>, NEVER use type_text for URLs. type_text is only for typing into document/form fields.\n"
            f"- For browser interaction: open_app msedge → focus_window Edge → open_url <url> → type_text/click\n"
            f"- IMPORTANT: The active Windows user is '{username}'. Do NOT hardcode placeholder paths like 'C:\\Users\\User' in skill arguments. Use the correct active user profile directory: '{home_dir}' or save relative to the current workspace directory.\n"
            f"Output ONLY the JSON array, nothing else."
        )

        for plan_attempt in range(1 + self._PLAN_RETRIES):
            self._log(f"Planning task (attempt {plan_attempt+1}): {task[:80]}")
            raw = self.brain.generate_simple(plan_prompt, max_tokens=800, temperature=0.2)
            if not raw:
                self._log(f"Plan attempt {plan_attempt+1}: LLM returned empty")
                if plan_attempt < self._PLAN_RETRIES:
                    self._log("Retrying plan...")
                    continue
                return [], True

            # Extract JSON array using json.JSONDecoder
            start_idx = raw.find('[')
            if start_idx == -1:
                self._log(f"Plan attempt {plan_attempt+1}: no JSON array in: {raw[:200]}")
                if plan_attempt < self._PLAN_RETRIES:
                    continue
                return [], True

            try:
                decoder = json.JSONDecoder()
                steps_raw, _ = decoder.raw_decode(raw, start_idx)
            except json.JSONDecodeError as exc:
                self._log(f"Plan attempt {plan_attempt+1}: JSON parse failed: {exc}")
                if plan_attempt < self._PLAN_RETRIES:
                    continue
                return [], True

            steps = []
            for i, s in enumerate(steps_raw[:MAX_STEPS]):
                steps.append(AgentStep(
                    index=i + 1,
                    description=str(s.get("description", f"Step {i+1}")),
                    skill=str(s.get("skill", "")).strip(),
                    args=str(s.get("args", "")).strip(),
                ))

            # ── Validate skill names against registry ──────────────────
            valid_skills = set(self.skill_engine.skills.keys())
            validated = []
            for step in steps:
                if step.skill in valid_skills:
                    validated.append(step)
                else:
                    closest = self.skill_engine._fuzzy_match_skill(step.skill)
                    if closest:
                        self._log(f"Planner wrote '{step.skill}' -> corrected to '{closest}'")
                        step.skill = closest
                        validated.append(step)
                    else:
                        self._log(f"Planner hallucinated skill '{step.skill}' -- dropping step")
            steps = validated
            # Re-index after filtering
            for i, step in enumerate(steps):
                step.index = i + 1

            self._log(f"Plan: {len(steps)} steps parsed")
            return steps, False

        return [], True

    # ── Phase 2: EXECUTE ──────────────────────────────────────────────────

    def _needs_confirmation(self, step: AgentStep) -> bool:
        """Return True if this step touches destructive actions."""
        return step.skill in _DESTRUCTIVE_SKILLS

    def _confirm_with_user(self, step: AgentStep) -> bool:
        """
        Ask the user for confirmation before a destructive step.
        Returns True if confirmed, False if denied/skipped.
        """
        prompt = (
            f"Step {step.index}: {step.description}. "
            f"This will use '{step.skill}' — shall I proceed? Say yes or no."
        )
        self.speak_fn(prompt)
        self.print_fn(f"[agentic] Confirm: {prompt}")

        if self.confirm_fn is None:
            return True   # no confirm channel — auto-approve

        response = self.confirm_fn("Confirm > ")
        if response is None:
            return False
        return response.strip().lower() in {"yes", "y", "yeah", "yep", "proceed", "do it", "ok", "okay"}

    def _execute_step(self, step: AgentStep) -> str:
        """Run the skill for this step. Returns result string."""
        fn = self.skill_engine.skills.get(step.skill)
        if fn is None:
            return f"Unknown skill: {step.skill}"
        try:
            result = fn(step.args)
            return _truncate_voice(result or "(no output)")
        except Exception as exc:
            return f"Skill error: {exc}"

    # ── Code self-fix loop ─────────────────────────────────────────────────

    def _try_fix_code(self, step: AgentStep) -> str:
        """
        When run_code returns an error, send code+error to LLM for a fix,
        then re-run. Repeats up to _CODE_FIX_MAX times.

        Improvements over naive loop:
          - Tracks best_result (shortest error = least noise) across attempts
          - Breaks immediately if LLM is unreachable (no 3×30s timeout spin)
          - Clean success still returns on the first passing attempt
        """
        result = step.result
        if step.skill != "run_code" or ":" not in step.args:
            return result
        if not any(ind in result for ind in _CODE_ERROR_INDICATORS):
            return result  # not an error — nothing to fix

        lang, code = step.args.split(":", 1)
        best_result = result  # original error is the baseline

        for fix_num in range(1, _CODE_FIX_MAX + 1):
            self._log(f"  Code self-fix {fix_num}/{_CODE_FIX_MAX}")
            self.print_fn(
                f"[agentic]   Error detected — auto-fixing (attempt {fix_num})"
            )

            fix_prompt = (
                f"This {lang} code produced an error. Fix it and return "
                f"ONLY the corrected code — no explanations, no markdown.\n\n"
                f"CODE:\n{code}\n\n"
                f"ERROR:\n{result}\n\n"
                f"Return ONLY the fixed code."
            )
            fixed = self.brain.generate_simple(
                fix_prompt, max_tokens=600, temperature=0.2
            )
            if not fixed:
                # LLM unreachable — stop immediately, don't burn more timeouts
                self._log("  LLM unreachable during code fix — aborting self-fix")
                break

            # Strip markdown fences the LLM may wrap code in.
            # Use regex so trailing explanations ("This prints 1.") after
            # the closing ``` don't prevent the fence from being removed.
            fixed = fixed.strip()
            fence_match = re.search(
                r'```(?:python|js|javascript|bash|sh)?\s*\n(.*?)```',
                fixed, re.DOTALL | re.IGNORECASE,
            )
            if fence_match:
                fixed = fence_match.group(1).strip()
            elif fixed.startswith("```"):
                # Opening fence only (no closing) — strip opener
                fixed = re.sub(r'^```\w*\s*\n?', '', fixed).strip()
            if not fixed:
                break

            # Update step and re-run
            code = fixed
            step.args = f"{lang}:{code}"
            self._log(f"  Auto-fixed code:\n{code[:300]}")
            fn = self.skill_engine.skills.get("run_code")
            if fn is None:
                break
            try:
                result = fn(step.args)
                result = _truncate_voice(result or "(no output)")
            except Exception as exc:
                result = f"Skill error: {exc}"

            self.print_fn(f"[agentic]   fix #{fix_num} result: {result[:120]}")

            # Clean success — return immediately
            if not any(ind in result for ind in _CODE_ERROR_INDICATORS):
                self._log(f"  Code fixed on attempt {fix_num}")
                return result

            # Still failing — keep the shorter error (less noise = closer to fix)
            if len(result) < len(best_result):
                best_result = result
                self._log(f"  New best result (shorter error: {len(result)} chars)")

        return best_result  # return least-noisy error across all attempts

    # ── Phase 3: VERIFY (Oracle + LLM fallback) ─────────────────────────

    # ── Success indicators: skip LLM verify for trivially-ok results ──────
    _AUTO_OK_PATTERNS = [
        "Opened ", "Waited ", "Typed ", "Pressed ", "Clicked ",
        "Scrolled ", "Focused: ", "Copied to clipboard", "Written ",
        "Playing top result", "Searching ", "(no output)",
    ]

    def _verify_with_oracle(
        self, step: AgentStep, task: str, results_so_far: List[str],
        pre_state: dict | None = None,
    ) -> str:
        """
        State Oracle verification (Phase 5 architecture).
        Routes to deterministic oracle first; falls back to LLM only
        when oracle returns AMBIGUOUS.

        Returns: 'ok' | 'retry:<hint>' | 'fail:<reason>' | 'done'
        """
        oracle_type = state_oracle.get_oracle_type(step.skill)
        verdict, reason = state_oracle.verify(
            step.skill, step.args, step.result, pre_state
        )
        self._log(
            f"Oracle [{oracle_type}] step {step.index}: "
            f"{verdict.upper()} — {reason[:80]}"
        )

        if verdict == state_oracle.OK:
            return STEP_OK
        if verdict == state_oracle.FAIL:
            return f"fail:{reason}"
        if verdict == state_oracle.RETRY:
            return f"retry:{reason}"

        # AMBIGUOUS — fall back to LLM (rare, ~22% of steps)
        self._log(f"Oracle ambiguous — falling back to LLM verify")
        return self._verify_llm(step, task, results_so_far)

    def _verify_llm(self, step: AgentStep, task: str, results_so_far: List[str]) -> str:
        """
        LLM-based verification fallback. Only called when the
        deterministic oracle cannot decide (AMBIGUOUS).
        """
        context = "\n".join(
            f"Step {i+1} result: {r[:200]}" for i, r in enumerate(results_so_far)
        )
        verify_prompt = (
            f"You are verifying a step in an agentic task.\n"
            f"TASK: {task}\n"
            f"Current step ({step.index}): {step.description}\n"
            f"Step result: {step.result[:300]}\n"
            f"Previous results:\n{context}\n\n"
            f"IMPORTANT: Judge ONLY by the step result text above.\n"
            f"If the result says 'Opened', 'Focused', 'Typed', etc. — it SUCCEEDED.\n"
            f"Do NOT assume failure based on your own reasoning.\n"
            f"Trust the result text.\n\n"
            f"Reply with exactly one of:\n"
            f"  OK          — step succeeded, continue to next\n"
            f"  RETRY:<hint> — step clearly failed (error message in result)\n"
            f"  FAIL:<reason> — unrecoverable failure\n"
            f"  DONE        — task is fully complete (no more steps needed)\n"
            f"Reply ONLY with one of the above, nothing else."
        )

        verdict = self.brain.generate_simple(verify_prompt, max_tokens=80, temperature=0.1)
        if not verdict:
            # LLM unreachable — fall back to string oracle (deterministic)
            # instead of blindly returning OK
            self._log(f"LLM verify unreachable — falling back to string oracle")
            sv, _sr = state_oracle.verify(step.skill, step.args, step.result)
            if sv == state_oracle.FAIL:
                return f"fail:string oracle detected failure"
            return STEP_OK  # string oracle says OK or AMBIGUOUS — optimistic

        verdict = verdict.strip().upper()
        self._log(f"LLM verify step {step.index}: {verdict[:60]}")

        if verdict.startswith("DONE"):
            return "done"
        if verdict.startswith("OK"):
            return STEP_OK
        if verdict.startswith("RETRY"):
            hint = verdict[6:].strip(": ") if ":" in verdict else ""
            return f"retry:{hint}"
        if verdict.startswith("FAIL"):
            reason = verdict[5:].strip(": ") if ":" in verdict else "unknown"
            return f"fail:{reason}"
        return STEP_OK

    # ── Phase 4: DONE / SUMMARY ───────────────────────────────────────────

    def _summarize(self, task: str, steps: List[AgentStep]) -> str:
        """Ask LLM for a short voice-friendly summary of what was done."""
        executed  = [s for s in steps if s.status != STEP_PENDING]
        completed = [s for s in steps if s.status in (STEP_OK, "done")]
        failed    = [s for s in steps if s.status == STEP_FAIL]

        steps_detail = "\n".join(
            f"Step {s.index} ({s.skill}): {s.result[:150]}"
            for s in executed
        )
        summary_prompt = (
            f"Summarize what FRIDAY accomplished for the user in 1-2 short sentences.\n"
            f"TASK: {task}\n"
            f"Steps completed: {len(completed)}/{len(executed)}\n"
            f"Results:\n{steps_detail}\n\n"
            f"Rules: no markdown, no lists, speak naturally, be concise."
        )
        summary = self.brain.generate_simple(summary_prompt, max_tokens=120, temperature=0.4)
        if not summary:
            if failed:
                return f"I completed {len(completed)} of {len(executed)} steps. Some steps could not be finished."
            return f"Done. Completed {len(completed)} step{'s' if len(completed) != 1 else ''}."
        return summary.strip()

    # ── Main run loop ──────────────────────────────────────────────────────

    def run(self, task: str) -> str:
        """
        Execute the full Plan→Execute→Verify→Done agentic loop.
        Returns a summary string (also spoken via speak_fn).
        """
        self.print_fn(f"\n[agentic] ─── Starting agentic task ───")
        self.print_fn(f"[agentic] Task: {task}")

        # ── Plan ──────────────────────────────────────────────────────────
        steps, plan_failed = self._plan(task)

        if not steps:
            if plan_failed:
                # Ollama is unreachable — tell user instead of silently failing
                self._log("Plan failed: Ollama unreachable after retries")
                degraded = (
                    "I could not reach my planning engine right now. "
                    "The local model may be busy or unresponsive. "
                    "I'll try to answer directly instead."
                )
                self.speak_fn(degraded)
                self.print_fn(f"[agentic] {degraded}")
                return ""  # caller falls back to normal LLM response
            self._log("No steps planned — falling back to direct answer")
            return ""   # caller will handle as normal LLM response

        self.print_fn(f"[agentic] Plan: {len(steps)} steps")
        plan_intro = f"I have a {len(steps)}-step plan. "
        for s in steps:
            plan_intro += f"Step {s.index}: {s.description}. "
        plan_intro += "Starting now."
        self.speak_fn(plan_intro)

        # ── Execute + Verify loop ─────────────────────────────────────────
        results_so_far: List[str] = []
        early_done = False
        consecutive_fails = 0  # abort if too many steps fail in a row
        _MAX_CONSECUTIVE_FAILS = 3

        for step in steps:
            self.print_fn(f"\n[agentic] Step {step.index}/{len(steps)}: {step.description}")
            self.print_fn(f"[agentic]   skill={step.skill}  args={step.args[:80]}")

            # Confirmation gate for destructive ops — announce BEFORE so user knows what's about to happen
            if self._needs_confirmation(step):
                confirmed = self._confirm_with_user(step)
                if not confirmed:
                    step.status = STEP_SKIP
                    step.result = "Skipped by user."
                    results_so_far.append(step.result)
                    # Muted intermediate speak to avoid latency
                    self._log(f"Skipping step {step.index}.")
                    continue
            else:
                # Muted intermediate speak to avoid latency
                self._log(f"Step {step.index}: {step.description}.")

            # Capture pre-state for file oracle (mtime comparison)
            pre_state = state_oracle.capture_pre_state(step.skill, step.args)

            # Execute with retry support
            for attempt in range(_MAX_STEP_RETRIES + 1):
                step.result = self._execute_step(step)

                # Code self-fix loop: if run_code errored, auto-fix before verify
                if step.skill == "run_code":
                    step.result = self._try_fix_code(step)
                    # Use execution oracle for run_code
                    v, reason = state_oracle.verify(
                        step.skill, step.args, step.result
                    )
                    self._log(f"Oracle [execution] step {step.index}: {v.upper()} — {reason[:80]}")
                    if v == state_oracle.FAIL:
                        step.status = STEP_FAIL
                    elif v == state_oracle.RETRY:
                        step.status = STEP_RETRY
                    else:
                        step.status = STEP_OK
                    results_so_far.append(step.result)
                    self.print_fn(f"[agentic]   result: {step.result[:120]}")
                    if step.status == STEP_FAIL:
                        self._log(f"Step {step.index} failed: could not run the code.")
                    else:
                        self._log(f"Step {step.index} done: code executed successfully.")
                    break

                self.print_fn(f"[agentic]   result: {step.result[:120]}")

                # ── Oracle verification (deterministic, no LLM) ──
                verdict = self._verify_with_oracle(step, task, results_so_far, pre_state)

                if verdict == "done":
                    step.status = STEP_OK
                    results_so_far.append(step.result)
                    self._log(f"Step {step.index} done. Task complete.")
                    early_done = True
                    break
                elif verdict == STEP_OK:
                    step.status = STEP_OK
                    results_so_far.append(step.result)
                    brief = step.result[:80] if len(step.result) <= 80 else step.result[:77] + "..."
                    self._log(f"Step {step.index} done: {brief}")
                    break
                elif verdict.startswith("retry") and attempt < _MAX_STEP_RETRIES:
                    hint = verdict[6:] if ":" in verdict else ""
                    self._log(f"  Retrying (attempt {attempt+1}): {hint}")
                    step.retries += 1
                    if hint:
                        step.hint = hint
                elif verdict.startswith("fail") or attempt >= _MAX_STEP_RETRIES:
                    reason = verdict[5:] if ":" in verdict else "unknown"
                    step.status = STEP_FAIL
                    step.result = f"Failed: {reason}"
                    results_so_far.append(step.result)
                    self._log(f"Step {step.index} failed: {reason[:60]}.")
                    break

            if early_done:
                self._log("Early exit: task complete before all steps ran")
                break

            # Track consecutive failures for early abort
            if step.status == STEP_FAIL:
                consecutive_fails += 1
                if step.skill in _ABORT_CHAIN_ON_FAIL:
                    self._log(f"Critical step failure: '{step.skill}' failed. Aborting the entire task chain.")
                    self._log(f"Critical step failed: could not {step.skill.replace('_', ' ')}. Aborting the remaining steps.")
                    break
                if consecutive_fails >= _MAX_CONSECUTIVE_FAILS:
                    self._log(
                        f"Aborting: {consecutive_fails} steps failed in a row"
                    )
                    self._log(
                        f"{consecutive_fails} steps failed in a row. Stopping to avoid wasting time."
                    )
                    break
            else:
                consecutive_fails = 0

        # ── Summary ───────────────────────────────────────────────────────
        self.print_fn(f"\n[agentic] ─── Task complete ───")
        summary = self._summarize(task, steps)
        self.print_fn(f"[agentic] Summary: {summary}")
        self.speak_fn(summary)
        return summary


# ── Agentic task detector ──────────────────────────────────────────────────

_AGENTIC_PATTERNS = [
    r'\b(create|write|make|build)\s+.+\s+(and|then)\s+(run|execute|test|open)',
    r'\b(find|search)\s+.+\s+(and|then)\s+(open|read|show|edit)',
    r'\bstep by step\b',
    r'\bmultiple steps?\b',
    r'\b(first|then|after that|finally)\b.{0,20}\b(open|create|write|run|search|click|type|edit|delete|install)\b',
    r'\b(read|open)\s+.+\s+(and|then)\s+(edit|modify|change|fix|write)',
    r'\bgit\s+(status|diff|log)\s+(and|then)',
    r'\brun\s+(the\s+)?(code|script|file)\b',
    r'\bcheck\s+.+\s+(and|then)\s+(fix|update|change)',
    r'\bdo\s+everything\b',
    r'\bautomate\b',
    # Phase 4+: Desktop automation & browser interaction
    r'\b(open|launch)\s+.+\s+(and|then)\s+(type|write|click|search|enter)',
    r'\btype\s+.+\s+(in|into|on)\s+',
    r'\b(go to|navigate to|open)\s+.+\s+(and|then)\s+',
    r'\bopen\s+\w+\s+(and|then)\s+(write|type|search|enter|send)\b',
    r'\b(focus|switch to)\s+.+\s+(and|then)',
    r'\bpowershell\b.*\b(and|then)\b',
    r'\b(install|uninstall)\s+\w+\s+(and|then|using)',
]

_AGENTIC_RE = re.compile("|".join(_AGENTIC_PATTERNS), re.IGNORECASE)

# Fast LLM classifier prompt — ~30 tokens response, ~200ms on local Ollama
_AGENTIC_CLASSIFIER_PROMPT = (
    "Does this user request require performing MULTIPLE computer actions "
    "(like opening apps, writing files, running code, browser automation, "
    "or system commands) in sequence?\n\n"
    "REQUEST: {input}\n\n"
    "Rules:\n"
    "- Answer YES only if the user wants you to DO multiple real computer "
    "actions (open app, write file, run code, click, type, etc.)\n"
    "- Answer NO if it's a question, casual conversation, single action, "
    "or a request for information/explanation\n"
    "- 'run the code' alone = NO (single action, not multi-step)\n"
    "- 'type something in Python' = NO (asking about Python, not automation)\n"
    "- 'go to sleep' = NO (shutdown command, not multi-step)\n"
    "- 'open notepad and type hello' = YES (open + type = multi-step)\n"
    "- 'create a file and run it' = YES (create + run = multi-step)\n\n"
    "Reply with ONLY 'YES' or 'NO'."
)


def is_agentic_task(user_input: str, brain=None) -> bool:
    """
    Two-gate agentic task detector:
      Gate 1 (regex): fast pattern match — cheap, catches obvious multi-step
      Gate 2 (LLM):   30-token yes/no classifier — prevents false fires on
                      conversational input that accidentally matches regex

    If brain is None (no LLM available), falls back to regex-only.
    """
    # Gate 1: regex pre-filter — if this doesn't match, definitely not agentic
    if not _AGENTIC_RE.search(user_input):
        return False

    # Gate 2: LLM confirmation — prevents false-fires on casual conversation
    if brain is not None:
        try:
            prompt = _AGENTIC_CLASSIFIER_PROMPT.format(input=user_input[:200])
            verdict = brain.generate_simple(
                prompt, max_tokens=5, temperature=0.0, retries=0,
            )
            if verdict:
                answer = verdict.strip().upper()
                if answer.startswith("NO"):
                    return False
                # YES or ambiguous → proceed with agentic
        except Exception:
            pass  # LLM failed — fall through to regex-only verdict

    return True

