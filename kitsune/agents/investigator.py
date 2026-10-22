"""
Issue Investigator — debugging agent that uses Kitsune 2.0's hybrid search.

Improvements over Kitsune 1.0:
- Uses query routing (S10) for intelligent retrieval.
- Multi-hop graph traversal (S2) for deeper root cause analysis.
- Impact analysis for understanding blast radius.
- Concept-level context (S12).
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

from kitsune_2_0.query.hybrid_search import HybridSearcher
from kitsune_2_0.query.router import route_query
from kitsune_2_0.query.synthesizer import Synthesizer

logger = logging.getLogger(__name__)

INVESTIGATOR_PROMPT = """You are an elite AI Debugging Engineer using Kitsune 2.0.
You have been assigned a bug report to investigate.

Below is the context retrieved from the repository's Knowledge Graph.
It contains:
1. PRIMARY CHUNKS: Code blocks most relevant to the bug (semantic match).
2. GRAPH DEPENDENCIES: Code that the primary chunks call (structural match, multi-hop).
3. KEYWORD MATCHES: Code containing exact keyword matches.

YOUR TASK:
1. Identify the Root Cause: Read the code and determine exactly why the bug is happening.
2. Pinpoint the Location: State exactly which file and function is causing the issue.
3. Propose a Fix: Explain how you would rewrite or fix the code to resolve the bug.
4. Impact Analysis: Note what other functions might be affected by the fix.

RULES:
- Be incredibly precise.
- Only base your reasoning on the provided code context.
- Format your response with clear headers: "Root Cause", "Location", "Proposed Fix", "Impact".
"""


class IssueInvestigator:
    """Agent that investigates bug reports using Kitsune 2.0.

    Usage
    -----
    investigator = IssueInvestigator()
    for chunk in investigator.investigate("login fails for OAuth users"):
        print(chunk, end="")
    """

    def __init__(self, searcher: HybridSearcher | None = None):
        self.searcher = searcher or HybridSearcher()
        self.synthesizer = Synthesizer()

    def investigate(self, bug_report: str) -> Iterator[str]:
        """Investigate a bug report and stream the analysis.

        Parameters
        ----------
        bug_report:
            Description of the bug.

        Yields
        ------
        Response text chunks as they arrive from the LLM.
        """
        logger.info(f"Investigating bug report: '{bug_report[:50]}...'")

        # Route the query for intelligent retrieval
        plan = route_query(bug_report)
        search_results = self.searcher.search(bug_report, k_primary=plan.top_k, plan=plan)

        # Format context
        context_str = self.synthesizer.format_context(search_results)
        user_message = f"BUG REPORT:\n{bug_report}\n\nREPOSITORY CONTEXT:\n{context_str}"

        # Stream the LLM response
        from openai import OpenAI
        client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=os.getenv("GROQ_API_KEY", ""),
        )

        try:
            response = client.chat.completions.create(
                model=os.getenv("LLM_MODEL", "openai/gpt-oss-20b"),
                messages=[
                    {"role": "system", "content": INVESTIGATOR_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                stream=True,
                temperature=0.2,
                reasoning_effort="low",
            )

            for chunk in response:
                if chunk.choices[0].delta.content is not None:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            yield f"\n\n[Agent Error: {e}]"
