"""AGENTIC STAR Marketplace entrypoint.

The entrypoint is derived from `class:` in config/agent.yaml, not hardcoded: the
platform imports this module to start the Pod, and a name that does not match the
manifest kills the container at import with nothing but a traceback.
"""

from pathlib import Path

from src.graph.graph import Graph
from framework.utils.config_loader import load_agent_config
from shared.bootstrap.marketplace_app import run_agent_marketplace

if __name__ == "__main__":
    run_agent_marketplace(
        Graph,
        agent_name="cmn-c1-596",
        # Without this the runner builds `agent_cls()` with no arguments and every
        # knob in config/config.yaml is a promise with no delivery: the file ships
        # in the image, the YAML parses, the unit tests pass config in by hand, and
        # the running agent uses its hardcoded defaults regardless. `config=` was
        # added in wheel 1.0.3; before it there was no channel at all.
        config=load_agent_config(Path(__file__).resolve().parent),
    )
