"""Local CLI prototype for the Cala/Sayari router agent -- see core.py for
the actual agent logic (shared with entrypoint.py, the AgentCore Runtime
deploy of this same agent).

Usage:
    python agent/router_agent.py "..."
    # env vars needed -- see README.md in this directory:
    #   CLIENT_ID, CLIENT_SECRET, GATEWAY_URL, BEDROCK_MODEL_ID
"""

from __future__ import annotations

import os
import sys
import uuid

from core import ask


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
    )


if __name__ == "__main__":
    main()
