"""Gemini LLM provider using the google-genai SDK."""

from __future__ import annotations

import json
import os
from typing import Optional

from google import genai
from google.genai import types

from applyops.llm.base import (
    AgentAction,
    BaseLLM,
    parse_action,
)
from applyops.llm.claude import SYSTEM_PROMPT


class GeminiLLM(BaseLLM):
    def __init__(self, model: str = "", api_key: Optional[str] = None):
        key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        self.client = genai.Client(api_key=key) if key else None
        self.model = model if model else "gemini-2.5-flash"

    async def analyze_page(
        self,
        screenshot: bytes,
        page_state_text: str,
        memory_context: str,
        task_context: str,
    ) -> list[AgentAction]:
        if not self.client:
            raise ValueError("GOOGLE_API_KEY is not set. Please configure your API key.")

        prompt = (
            f"### TASK CONTEXT\n{task_context}\n\n"
            f"### MEMORY (Profile & Learned Answers)\n{memory_context}\n\n"
            f"### EXTRACTED PAGE DOM STATE\n{page_state_text}\n\n"
            "Analyze the screenshot image and DOM state, and return a JSON object with format: {\"actions\": [...]}. Return ONLY JSON."
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=screenshot, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
            ),
        )

        content = response.text or "{}"
        return self._parse_actions(content)

    async def match_memory(
        self,
        question: str,
        qa_entries: list[dict],
    ) -> Optional[str]:
        if not self.client or not qa_entries:
            return None

        prompt = (
            f"Question: \"{question}\"\n\n"
            "Learned Q&A:\n"
            + json.dumps(qa_entries, indent=2)
            + "\n\nIf one answers this question, respond with JSON: {\"match\": true, \"answer\": \"...\"}. Else {\"match\": false}."
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                system_instruction="You match job application questions with saved answers.",
                response_mime_type="application/json",
            ),
        )

        content = response.text or "{}"
        try:
            data = json.loads(content)
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
                        print(f"[GeminiLLM] Failed to parse action {item}: {err}")
        except Exception as e:
            print(f"[GeminiLLM] JSON parse error: {e}")

        return actions
