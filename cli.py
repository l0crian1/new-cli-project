#!/usr/bin/env python3
import json
from pathlib import Path
from typing import Dict, Any

from prompt_toolkit import PromptSession

from cli_common import (
    CommandNavigator,
    TreeAutoSuggest,
    FishKeyBindings,
)

JSON_PATH = Path("op.json")


# ---------------- Core tree utilities (project-specific I/O) ----------------

def load_tree() -> Dict[str, Any]:
    with JSON_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


"""Interactive CLI harness for the operation tree.

Wires up the prompt session with autosuggest and keybindings, and
dispatches user input to the CommandNavigator for execution.
"""

# ---------------- Main CLI ----------------

def main():
    """Start the interactive CLI loop and dispatch commands via CommandNavigator."""
    tree = load_tree()
    navigator = CommandNavigator(tree)

    session = PromptSession(
        auto_suggest=TreeAutoSuggest(navigator),
        key_bindings=FishKeyBindings(navigator, prompt_label="router> ").get()
    )

    while True:
        try:
            user_line = session.prompt("router> ")
        except KeyboardInterrupt:
            # Clear current line and return to a fresh prompt
            continue
        except EOFError:
            print("\nExiting CLI...")
            break

        for raw_line in user_line.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            tokens = line.split()
            if not tokens:
                continue
            if tokens[0].lower() == "exit":
                print("Goodbye")
                return

            resolved_node, consumed_path, error_token = navigator.resolve_path(tokens)
            if error_token is not None or resolved_node is None:
                path_str = " ".join(consumed_path) if consumed_path else "(root)"
                print(f"Unknown token '{error_token}' at {path_str}")
                continue

            navigator.execute_current_command(resolved_node, line)


if __name__ == "__main__":
    main()
