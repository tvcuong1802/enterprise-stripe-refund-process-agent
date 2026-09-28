"""One reader for config/config.yaml. Byte-identical fleet-wide.

Two things this exists to prevent, both measured:

1. **A knob declared and never read.** `marketplace_app.py:95` is `agent = agent_cls()` --
   no config is passed, and the invoke path carries none either. The Dockerfile still
   copies `config/`, so every value in config.yaml is a promise the runtime never keeps
   unless something opens the file. Measured on (internal reference removed): `grounding_threshold` stayed
   0.7 whatever the operator set. No gate caught it -- the file exists, the YAML parses,
   and the tests pass config in by hand.

2. **Paths resolved from the working directory.** The container runs from `/app`; pytest
   runs from wherever it was invoked. `Path("config/config.yaml")` therefore works in the
   image and silently returns {} in a test -- or the reverse. The path here is derived
   from `__file__`, so it is the same file in both.

Explicit config WINS OUTRIGHT -- the file is read only when the caller passed nothing.
A merge that used the file as a base made `{"agent": {"config": {}}}` -- what the
fail-closed tests pass -- inherit real values, and three tests quietly stopped testing
what they were written for. *Absent* and *deliberately empty* are different answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

_cache: dict[str, Any] | None = None


def app_config(explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    """The caller\'s config if it gave one, else config/config.yaml, else {}."""
    if explicit is not None:
        return dict(explicit)
    global _cache
    if _cache is None:
        loaded: dict[str, Any] = {}
        try:
            # importlib, not `import yaml`: PyYAML ships no type stubs, and CI's
            # `mypy src/` fails with "Library stubs not installed for yaml" on every repo
            # whose dev extras omit types-PyYAML -- measured on five repos at once,
            # 2026-09-03, while local mypy was clean because the stub happened to be
            # installed there. Adding the stub to thirteen pyprojects fixes it thirteen
            # times; not needing it fixes it once. The module is optional here anyway.
            import importlib  # noqa: PLC0415

            yaml = importlib.import_module("yaml")
            if _CONFIG_PATH.exists():
                parsed = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    loaded = parsed
        except Exception:  # noqa: BLE001 -- unreadable config falls back to node defaults
            loaded = {}
        _cache = loaded
    return dict(_cache)


def llm_settings(explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    """The subset of config.yaml that `build_llm_client()` forwards to the client.

    `timeout_s` / `timeout_seconds` are the scaffold\'s names for the same knob; the
    client takes `timeout`. Translating here rather than at each call site is what keeps
    this file identical across repos.
    """
    cfg = app_config(explicit)
    settings: dict[str, Any] = {}
    # Written as direct lookups rather than a loop over key names: a reader -- human or
    # gate -- looking for "who reads timeout_s" finds it by searching for the key, which
    # is how the question is actually asked.
    timeout = cfg.get("timeout_s")
    if timeout is None:
        timeout = cfg.get("timeout_seconds")
    if timeout is None:
        timeout = cfg.get("timeout")
    if timeout is not None:
        settings["timeout"] = timeout
    llm_block = cfg.get("llm")
    if isinstance(llm_block, dict):
        for key in ("max_tokens", "temperature", "top_p", "seed"):
            if llm_block.get(key) is not None:
                settings[key] = llm_block[key]
    return settings
