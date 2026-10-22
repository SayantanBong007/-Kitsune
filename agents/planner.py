"""
Fix Planner — generates structured fix payloads for PR comments.

Takes a Root Cause Analysis from the Investigator and generates a Markdown
payload with before/after code blocks.

CRITICAL SAFETY GUARANTEE:
This agent NEVER writes to source files directly. It purely returns a
payload that ArchFox posts to a GitHub PR for Human-in-the-Loop review.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """You are an elite AI Fix Planner using Kitsune.
You have been handed a Root Cause Analysis for a bug, along with the actual PR diff.

YOUR TASK:
Generate the exact Markdown payload that will be posted as a comment on a GitHub Pull Request.
This comment must clearly explain the fix and provide the before/after code blocks.

FORMAT EXPECTED:
# ArchFox Bug Fix Proposal

**Root Cause Summary:**
[Brief 1-2 sentence summary of what was found]

**Target File:** `[relative file path]`
**Target Line:** [Line number of an added line (+) from the PR diff]

**Proposed Change:**
```diff
- [old code]
+ [new code]
```

**Impact:**
[Brief note on what other functions might be affected]

RULES:
- Be concise.
- Output ONLY the Markdown payload. Do not include any conversational text.
- The diff should only include the specific lines being changed, plus context.
- The Target File must be a relative path matching the PR diff.
- The Target Line MUST be an added line (+) from the PR diff.
"""


class FixPlanner:
    """Agent that translates an investigation into a PR Comment Payload."""

    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=os.getenv("GROQ_API_KEY", ""),
        )
        self.model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")

    def generate_payload(self, investigation_result: str, pr_diff: str = "") -> Iterator[str]:
        """Generate the PR Comment Payload and stream it.

        Parameters
        ----------
        investigation_result:
            The root cause analysis from the IssueInvestigator.
        pr_diff:
            The PR diff to determine target file and line.

        Yields
        ------
        Response text chunks as they arrive from the LLM.
        """
        logger.info("Generating Fix Payload for ArchFox...")

        user_message = (
            f"PR DIFF (use this to determine Target File and Target Line):\n{pr_diff}\n\n"
            f"ROOT CAUSE ANALYSIS FROM INVESTIGATOR:\n{investigation_result}"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PLANNER_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                stream=True,
                temperature=0.1,
                reasoning_effort="low",
            )

            for chunk in response:
                if chunk.choices[0].delta.content is not None:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            yield f"\n\n[Planner Error: {e}]"
