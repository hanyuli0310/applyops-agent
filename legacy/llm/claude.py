"""Claude LLM provider using Anthropic API."""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from anthropic import AsyncAnthropic

from applyops.llm.base import (
    ACTION_MAP,
    AgentAction,
    BaseLLM,
    Done,
    ScrollDown,
    parse_action,
)

SYSTEM_PROMPT = """You are an expert AI job application assistant.
Your goal is to inspect the current web page (screenshot and extracted DOM information) and decide the exact sequential browser actions needed to advance or complete a job application on platforms like LinkedIn, Indeed, Workday, Greenhouse, Lever, etc.

You have access to the user's Profile, Learned Q&A memory, and Known Selectors for the current platform.
Rules:
1. If the job page has an 'Easy Apply', 'Apply', 'Apply on company site' button, click it.
2. If form fields (Name, Email, Phone, Experience, Links, Work Auth, etc.) are present, fill them using the Candidate Profile or Learned Q&A. Never invent facts that are not in the profile or memory.
3. If an input requires a file upload (like resume), output an upload_file action with file_path='resume'.
4. When a Known Selectors list is provided for this platform, PREFER those selectors — they have been validated on previous runs. Try the first one, and fall back to the next one on failure.
5. If a question is asked and you cannot deduce the answer from the Candidate Profile or Learned Q&A, emit an 'ask_user' action with the question text, brief context, AND the 'selector' of the input/radio group it belongs to whenever one exists.
   Always emit ask_user even if you have a weak guess — the system will check memory first and may prefill your suggestion for the human to confirm.
6. If CAPTCHA, 2FA, or login walls are shown, emit a 'wait_for_user' action with a description.
7. When the final application has been submitted successfully (or a confirmation screen appears), emit a 'done' action with status='success'.
8. Only return valid JSON with a list of action objects under the 'actions' key. Return an empty list if you need the page to settle.

Action Object Schema examples:
- {"action": "click", "selector": "button.jobs-apply-button", "reason": "Click apply button"}
- {"action": "fill_field", "selector": "#name-input", "value": "John Doe", "reason": "Fill name"}
- {"action": "select_option", "selector": "#country-select", "value": "United States", "reason": "Select country"}
- {"action": "check_box", "selector": "#agree-terms", "checked": true, "reason": "Agree to terms"}
- {"action": "upload_file", "selector": "input[type=file]", "file_path": "resume", "reason": "Upload resume"}
- {"action": "ask_user", "question": "Are you willing to relocate to Seattle?", "context": "Form relocation question", "selector": "#relocate-radio-group"}
- {"action": "wait_for_user", "reason": "Solve Cloudflare CAPTCHA"}
- {"action": "scroll_down", "reason": "Scroll to find submit button"}
- {"action": "done", "status": "success", "summary": "Application submitted"}
"""


class ClaudeLLM(BaseLLM):
    def __init__(self, model: str = "", api_key: Optional[str] = None):
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.client = AsyncAnthropic(api_key=key) if key else None
        # claude-3-7-sonnet was retired on 2026-02-19; Sonnet 4.6 is the
        # current recommended replacement for that family.
        self.model = model if model else "claude-sonnet-4-6"

    async def analyze_page(
        self,
        screenshot: bytes,
        page_state_text: str,
        memory_context: str,
        task_context: str,
    ) -> list[AgentAction]:
        if not self.client:
            raise ValueError("ANTHROPIC_API_KEY is not set. Please configure your API key.")

        image_b64 = base64.b64encode(screenshot).decode("utf-8")

        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_b64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"### TASK CONTEXT\n{task_context}\n\n"
                    f"### MEMORY (Profile & Learned Answers)\n{memory_context}\n\n"
                    f"### EXTRACTED PAGE DOM STATE\n{page_state_text}\n\n"
                    "Now analyze the screenshot and DOM state, and return a JSON object with the format: "
                    '{"actions": [...]}. Return ONLY JSON.'
                ),
            },
        ]

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )

        content_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                content_text += block.text

        return self._parse_actions(content_text)

    async def match_memory(
        self,
        question: str,
        qa_entries: list[dict],
    ) -> Optional[str]:
        if not self.client or not qa_entries:
            return None

        prompt = (
            f"Given the application form question:\n\"{question}\"\n\n"
            "And previous question-answer pairs:\n"
            + json.dumps(qa_entries, indent=2)
            + "\n\nIf one of the answers directly answers this question, return JSON: {\"match\": true, \"answer\": \"...\"}.\n"
            "Otherwise return: {\"match\": false}."
        )

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            text = response.content[0].text
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                data = json.loads(text[start : end + 1])
                if data.get("match") and data.get("answer"):
                    return str(data["answer"])
        except Exception:
            pass

        return None

    def _parse_actions(self, raw_text: str) -> list[AgentAction]:
        actions: list[AgentAction] = []
        try:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1:
                parsed = json.loads(raw_text[start : end + 1])
                items = parsed.get("actions", [])
                for item in items:
                    try:
                        actions.append(parse_action(item))
                    except Exception as err:
                        print(f"[ClaudeLLM] Failed to parse action {item}: {err}")
        except Exception as e:
            print(f"[ClaudeLLM] JSON parse error: {e}\nRaw: {raw_text[:300]}")

        return actions
