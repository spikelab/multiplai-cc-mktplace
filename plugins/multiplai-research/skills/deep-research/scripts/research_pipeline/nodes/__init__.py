"""Pipeline nodes — each one is a focused async function.

Code nodes (search, parts of triage, fetcher-driven read) are pure Python.
LLM nodes (plan, diverge, challenge, relevance-score, extract, reassess,
synthesize, adversarial) call claude_agent_sdk via research_pipeline.sdk.
"""
