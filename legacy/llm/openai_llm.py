"""OpenAI LLM provider using OpenAI API with GPT-4o vision."""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from openai import AsyncOpenAI

from applyops.llm.base import (
    AgentAction,
    BaseLLM,
    parse_action,
)
from applyops.llm.claude import SYSTEM_PROMPT


class OpenAILLM(BaseLLM):
    def __init__(self, model: str = "", api_key: Optional[str] = None):
        key = api_key or os.getenv("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=key) if key else None
        self.model = model if model else "gpt-4o"

    async def analyze_page(
        self,
        screenshot: bytes,
        page_state_text: str,
        memory_context: str,
        task_context: str,
    ) -> list[AgentAction]:
        if not self.client:
            raise ValueError("OPENAI_API_KEY is not set. Please configure your API key.")

        image_b64 = base64.b64encode(screenshot).decode("utf-8")

        prompt = (
            f"### TASK CONTEXT\n{task_context}\n\n"
            f"### MEMORY (Profile & Learned Answers)\n{memory_context}\n\n"
            f"### EXTRACTED PAGE DOM STATE\n{page_state_text}\n\n"
            "Analyze the screenshot and DOM state, and return a JSON object with format: {\"actions\": [...]}. Return ONLY JSON."
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"
                            },
                        },
                    ],
                },
            ],
            max_tokens=2048,
        )

        content = response.choices[0].message.content or "{}"
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

        response = await self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You match job application questions with saved answers."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )

        content = response.choices[0].message.content or "{}"
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
            parsed = json.loads(raw_text)
            items = parsed.get("actions", [])
            for item in items:
                try:
                    actions.append(parse_action(item))
                except Exception as err:
                    print(f"[OpenAILLM] Failed to parse action {item}: {err}")
        except Exception as e:
            print(f"[OpenAILLM] JSON parse error: {e}")

        return actions
