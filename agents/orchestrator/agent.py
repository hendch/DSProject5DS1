"""The orchestrator: routes triggers to agents, waits for the guardrail and the humans,
then executes what was approved.

    engine.py     the run lifecycle (persisted in PostgreSQL)
    routing.py    rules first, LLM triage second, human triage last
    adapters.py   how each agent is called and what it proposes
    executors.py  what an approved proposal does
    github.py     following repositories: commits, CI and pull requests
    catalog.py    agents, routes and demo scenarios

Run the whole platform with:  python -m app
"""
from agents.orchestrator.engine import Orchestrator  # noqa: F401
