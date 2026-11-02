"""Lean `claude -p` calls on a Claude subscription: ~600 input tokens instead of 429k. See core.py for the measurements."""
from .core import (CONNECTORS_OFF_ENV, LEAN_ARGS, LEAN_ENV, STRIPPED_ENV, LeanClaude, TokenCeiling, build_command,
                   call_workdir, find_claude_cli, unwrap_cli_json)

__all__ = ["LeanClaude", "TokenCeiling", "build_command", "find_claude_cli", "call_workdir", "unwrap_cli_json",
           "LEAN_ARGS", "LEAN_ENV", "CONNECTORS_OFF_ENV", "STRIPPED_ENV"]
__version__ = "0.1.0"
