"""Abstract base class for LLM providers + action type definitions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel


# ── Agent Action Types ───────────────────────────────────────────────


class FillField(BaseModel):
    """Fill a form field with a value."""

    action: str = "fill_field"
    selector: str
    value: str
    reason: str = ""


class ClickElement(BaseModel):
    """Click a button or link."""

    action: str = "click"
    selector: str
    reason: str = ""


class SelectOption(BaseModel):
    """Select a dropdown option."""

    action: str = "select_option"
    selector: str
    value: str
    reason: str = ""


class UploadFile(BaseModel):
    """Upload a file (resume, cover letter, etc.)."""

    action: str = "upload_file"
    selector: str
    file_path: str
    reason: str = ""


class CheckBox(BaseModel):
    """Check or uncheck a checkbox."""

    action: str = "check_box"
    selector: str
    checked: bool = True
    reason: str = ""


class AskUser(BaseModel):
    """Ask the user a question the agent can't answer from memory.

    Include ``selector`` whenever the question maps to a concrete form control —
    that lets a remembered answer be applied immediately instead of waiting for
    another LLM round-trip.
    """

    action: str = "ask_user"
    question: str
    context: str = ""
    selector: str = ""


class WaitForUser(BaseModel):
    """Pause and wait for user to handle something (CAPTCHA, login, etc.)."""

    action: str = "wait_for_user"
    reason: str


class ScrollDown(BaseModel):
    """Scroll down to see more content."""

    action: str = "scroll_down"
    reason: str = ""


class Navigate(BaseModel):
    """Navigate to a different URL."""

    action: str = "navigate"
    url: str
    reason: str = ""


class Done(BaseModel):
    """Application is complete or has failed."""

    action: str = "done"
    status: str  # success, failed, paused
    summary: str = ""


# Union type for all possible actions
AgentAction = (
    FillField
    | ClickElement
    | SelectOption
    | UploadFile
    | CheckBox
    | AskUser
    | WaitForUser
    | ScrollDown
    | Navigate
    | Done
)

ACTION_MAP: dict[str, type[BaseModel]] = {
    "fill_field": FillField,
    "click": ClickElement,
    "select_option": SelectOption,
    "upload_file": UploadFile,
    "check_box": CheckBox,
    "ask_user": AskUser,
    "wait_for_user": WaitForUser,
    "scroll_down": ScrollDown,
    "navigate": Navigate,
    "done": Done,
}


def parse_action(data: dict) -> AgentAction:
    """Parse an action dict into the appropriate action model."""
    action_type = data.get("action", "")
    cls = ACTION_MAP.get(action_type)
    if cls is None:
        raise ValueError(f"Unknown action type: {action_type}")
    return cls.model_validate(data)


# ── Abstract LLM Interface ──────────────────────────────────────────


class BaseLLM(ABC):
    """Abstract base class for LLM providers.

    Implementations must provide:
    - analyze_page: look at a page and decide what actions to take
    - match_memory: semantically match a question to learned Q&A
    """

    @abstractmethod
    async def analyze_page(
        self,
        screenshot: bytes,
        page_state_text: str,
        memory_context: str,
        task_context: str,
    ) -> list[AgentAction]:
        """Analyze the current page and return a list of actions to take.

        Args:
            screenshot: PNG screenshot of the current page.
            page_state_text: Structured text description of form fields, buttons, etc.
            memory_context: The user's profile and learned Q&A as text.
            task_context: Context about the current task (job URL, platform, etc.)

        Returns:
            A list of AgentAction objects to execute.
        """
        ...

    @abstractmethod
    async def match_memory(
        self,
        question: str,
        qa_entries: list[dict],
    ) -> Optional[str]:
        """Semantically match a question to learned Q&A entries.

        Args:
            question: The question from the application form.
            qa_entries: List of learned Q&A dicts with 'question' and 'answer' keys.

        Returns:
            The matched answer, or None if no good match found.
        """
        ...
