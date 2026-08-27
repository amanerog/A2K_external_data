"""Local CLI prototype for the Cala/Sayari router agent -- see core.py for
the actual agent logic (shared with entrypoint.py, the AgentCore Runtime
deploy of this same agent).

Usage:
    python agent/router_agent.py "..."
    # env vars needed -- see README.md in this directory:
    #   CLIENT_ID, CLIENT_SECRET, GATEWAY_URL, BEDROCK_MODEL_ID
    # A2K_AGENT_DEBUG=false to silence the >>/<< tool-call lines (on by default
    # here -- this is the debugging entrypoint; entrypoint.py leaves debug off)
"""

from __future__ import annotations

import os
import sys
import uuid

from core import ask


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "¿Qué sabemos de Repsol?"
    ask(
        question,
        gateway_url=os.environ["GATEWAY_URL"],
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region=os.environ.get("AWS_REGION", "eu-west-1"),
        # No Runtime session/caller identity to inherit locally -- mint a fresh
        # session_id per run and label the client explicitly, so agent.tool_call
        # log lines and EMF metrics from local testing are still distinguishable
        # from real deployed traffic (see observability.py).
        session_id=str(uuid.uuid4()),
        internal_client="local-cli",
        debug=_bool_env("A2K_AGENT_DEBUG", True),
    )


if __name__ == "__main__":
    main()
