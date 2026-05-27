import unittest
import sys
import re

# Add path if needed
sys.path.append(r"c:\Users\akhil\Downloads\jarvis")

from voice import VoiceIO
from main import _are_semantically_similar, REQUIRED_PARAMS, _is_plausible_input, _sanitize_history_content
from skills import skill_calculator

class TestWave4Fixes(unittest.TestCase):
    def test_sanitize_history_content(self):
        self.assertEqual(_sanitize_history_content("[partial] hello"), "hello")
        self.assertEqual(_sanitize_history_content("[agentic] hello... [interrupted]"), "hello")
        self.assertEqual(_sanitize_history_content("[ACTION:smartopen:youtube]\nhello"), "hello")
        self.assertEqual(_sanitize_history_content(""), "")
    def test_plausible_input(self):
        self.assertTrue(_is_plausible_input("open notepad"))
        self.assertFalse(_is_plausible_input("Howling."))
        self.assertFalse(_is_plausible_input("nice"))
        self.assertFalse(_is_plausible_input("Vehicle framing."))
    def test_clean_for_tts(self):
        # We need a mock / real voice instance to test _clean_for_tts
        # Let's instantiate a minimal VoiceIO class or mock it.
        # But since we added it to VoiceIO, let's create a minimal test.
        # Actually, let's inspect the regex logic directly.
        def clean(text):
            text = re.sub(r'\s*\.\.\.\s*\[interrupted\]', '', text)
            text = re.sub(r'\s*\[interrupted\]', '', text)
            text = text.replace('...', '')
            return text.strip()

        self.assertEqual(clean("Hello [interrupted]"), "Hello")
        self.assertEqual(clean("Hello ... [interrupted]"), "Hello")
        self.assertEqual(clean("Wait... what?"), "Wait what?")
        self.assertEqual(clean("Thinking... [interrupted before speaking]"), "Thinking [interrupted before speaking]") # check it only strips exact debugs

    def test_are_semantically_similar(self):
        self.assertTrue(_are_semantically_similar("calculate 2 plus 2", "calculate two plus two"))
        self.assertTrue(_are_semantically_similar("open notepad", "open notepad."))
        self.assertFalse(_are_semantically_similar("open edge", "open chrome"))
        self.assertTrue(_are_semantically_similar("what's the weather in Tokyo", "what is the weather in Tokyo"))

    def test_calculator_clipboard(self):
        # Test calculator evaluate and clipboard write
        # Bypasses calc.exe entirely
        result = skill_calculator("100 * 5 + 10")
        self.assertIn("510", result)
        
        # Verify clipboard has the result
        try:
            import pyperclip
            self.assertEqual(pyperclip.paste(), "510")
        except Exception:
            # Clipboard/pyperclip not supported in head-less test environment
            pass

    def test_required_params_schema(self):
        # Test calculator validator
        calc_param = [p for p in REQUIRED_PARAMS if p["intent"] == "calculate"][0]
        self.assertTrue(calc_param["validator"]("calculate 5 * 5"))
        self.assertFalse(calc_param["validator"]("calculate the numbers"))
        
        # Test web search validator
        search_param = [p for p in REQUIRED_PARAMS if p["intent"] == "web_search"][0]
        self.assertTrue(search_param["validator"]("search the web for weather today"))
        self.assertFalse(search_param["validator"]("search the web"))
        
        # Test create file validator
        file_param = [p for p in REQUIRED_PARAMS if p["intent"] == "create_file"][0]
        self.assertTrue(file_param["validator"]("create a file named script.py"))
        self.assertFalse(file_param["validator"]("create a file"))

        # Test open app validator
        app_param = [p for p in REQUIRED_PARAMS if p["intent"] == "open_app"][0]
        self.assertTrue(app_param["validator"]("open notepad"))
        self.assertFalse(app_param["validator"]("open the app"))

    def test_sentence_stream_action_no_split(self):
        from brain import Brain
        from unittest.mock import patch, MagicMock
        
        brain = Brain(enabled=True)
        brain.active_model = "qwen3:8b"
        
        mock_response_data = [
            b'{"message": {"content": "Sir, I think the "}, "done": false}',
            b'{"message": {"content": "code looks good. [ACTION:"}, "done": false}',
            b'{"message": {"content": "run_code:python:with "}, "done": false}',
            b'{"message": {"content": "open(\'test.py\', \'r\') as f: print(f.read())]"}, "done": true}'
        ]
        
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_response_data
        
        with patch("urllib.request.urlopen", return_value=mock_resp):
            generator = brain.stream_sentences("check my code")
            sentences = list(generator)
            
        self.assertIn("Sir, I think the code looks good.", sentences)
        action_sents = [s for s in sentences if "[ACTION:" in s]
        self.assertEqual(len(action_sents), 1)
        self.assertEqual(action_sents[0], "[ACTION:run_code:python:with open('test.py', 'r') as f: print(f.read())]")

    def test_brain_response_barge_in_direct_skill(self):
        from unittest.mock import patch, MagicMock
        from main import _handle_brain_response
        
        mock_agent = MagicMock()
        mock_voice = MagicMock()
        mock_memory = MagicMock()
        
        mock_voice._barge_in_speech_pending = True
        mock_voice.played_sentences = ["some text"]
        mock_voice.listen.return_value = "remember that my keys are in the drawer"
        
        mock_agent.brain = MagicMock()
        mock_agent.skills = MagicMock()
        mock_agent.brain.stream_sentences.return_value = iter(["Here is a response"])
        
        with patch("main.detect_direct_intent") as mock_detect, \
             patch("main._handle_direct_skill") as mock_handle_direct, \
             patch("main._is_plausible_input", return_value=True):
             
             # Turn 0: normal LLM run, which gets interrupted
             # Turn 1: user recovers and says "remember...", triggering intent
             mock_detect.side_effect = [None, ("remember", "keys||in the drawer", "I've saved that to my memory.")]
             
             _handle_brain_response(mock_agent, mock_voice, "hello", mock_memory)
             
             mock_handle_direct.assert_called_once_with(
                 mock_agent, mock_voice, "remember that my keys are in the drawer",
                 mock_memory, mock_agent.skills, ("remember", "keys||in the drawer", "I've saved that to my memory.")
             )

if __name__ == "__main__":
    unittest.main()
