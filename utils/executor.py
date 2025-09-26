"""Command execution helpers decoupled from navigation.

Provides a thin wrapper around building a runnable command string from the
current node and input, interpolating tag values, and executing via utils.process.cmd.
"""

from __future__ import annotations

from typing import Dict, Any

from utils.process import cmd
from cli_common import CommandNavigator


def _format_command_with_tags(command: str, tag_values: Dict[str, str]) -> str:
    """Format a command string with captured tag values; leave unknown placeholders.

    Unknown placeholders are preserved in the output, e.g. '{missing}' stays
    '{missing}'.
    """
    try:
        class _Default(dict):
            def __missing__(self, key):  # type: ignore[override]
                return "{" + key + "}"

        return command.format_map(_Default(tag_values))
    except Exception:
        return command


def _run_and_print(command_to_execute: str) -> None:
    """Run a plaintext shell command and stream stdout/stderr to console.

    Uses utils.process.cmd() so authored strings like "vtysh -c '...'" execute as-is.
    Prints stdout directly; if the process exits non-zero, prints stderr as well.
    """
    result = cmd(command_to_execute)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0 and result.stderr:
        print(result.stderr, end="")


def execute_current_command(navigator: CommandNavigator, node: Dict[str, Any], raw_text: str) -> None:
    """Execute a node's bound command (or tagCommand), or report 'Incomplete command'.

    - If at a tagNode and its value was entered, prefer 'tagCommand' when present
    - Otherwise, run 'command' when present
    - If neither applies, emit an 'Incomplete command: <path>' message
    """
    tag_values = navigator.extract_tag_values(raw_text)

    if CommandNavigator.is_tag_node(node) and navigator.tag_value_entered_for_node(node, raw_text) and "tagCommand" in node:
        command_to_execute = _format_command_with_tags(node["tagCommand"], tag_values)
        _run_and_print(command_to_execute)
        return

    if "command" in node:
        command_to_execute = _format_command_with_tags(node.get("command", ""), tag_values)
        _run_and_print(command_to_execute)
        return

    # No runnable command here; report incomplete command with the consumed path
    path_tokens, _, _ = CommandNavigator.parse_input_tokens(raw_text)
    _, consumed_path, _ = navigator.resolve_path(path_tokens)
    path_str = " ".join(consumed_path) if consumed_path else "(root)"
    print(f"Incomplete command: {path_str}")
    return


