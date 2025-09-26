import shlex
from typing import Dict, List, Tuple, Optional, Any

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.application import run_in_terminal
from tabulate import tabulate
from utils.process import cmd
try:
    import value_help  # optional module for placeholder definitions
except Exception:  # noqa: BLE001
    value_help = None

META_KEYS = {"description", "type", "command", "values"}


class CommandNavigator:
    """
    Navigator and completion engine for the CLI tree.

    Responsibilities:
    - Resolve user tokens into a node in the operation tree
    - Determine valid completions under the current node (with tagNode rules)
    - Detect when tagNode values have been entered (including nested cases)
    - Interpolate captured tag values into commands on execution
    """

    def __init__(self, tree: Dict[str, Any]):
        self.tree = tree

    # ---- Static utilities ----
    @staticmethod
    def get_children(node: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Return only real child nodes (exclude metadata keys)."""
        return {
            key: value
            for key, value in node.items()
            if isinstance(value, dict) and key not in META_KEYS
        }

    @staticmethod
    def is_tag_node(node: Dict[str, Any]) -> bool:
        """True if the node expects a value token before exposing non-leaf children."""
        return isinstance(node, dict) and node.get("type") == "tagNode"

    @staticmethod
    def get_tag_placeholders(node: Dict[str, Any]) -> List[Tuple[str, str]]:
        """
        Resolve value placeholders for a tagNode into (placeholder, description) pairs.

        Supports two authoring styles inside op.json:
        - Inline lists per placeholder:
          ["<x.x.x.x/24>", "IPv4 route (format IP/CIDR)"]
        - External references into value_help.py by string key:
          "ipv4net" -> value_help.ipv4net = ["<x.x.x.x/24>", "...desc..."]

        Parameters:
        - node: A tag node mapping which may define a "values" list.

        Returns:
        - A list of (placeholder, description) tuples. Unknown/malformed entries
          are skipped. Plain string entries are surfaced as-is with empty
          descriptions if no value_help match is found.

        Examples:
        - values: [["<x.x.x.x>", "IPv4 address"]]
          -> [("<x.x.x.x>", "IPv4 address")]
        - values: ["ipv4addr"] with value_help.ipv4addr = ["<x.x.x.x>", "..."]
          -> [("<x.x.x.x>", "...")]
        """
        values = node.get("values")
        if not isinstance(values, list):
            return []
        out: List[Tuple[str, str]] = []
        for item in values:
            # Inline placeholder ["<...>", "desc"]
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                placeholder = str(item[0])
                desc = str(item[1]) if len(item) > 1 else ""
                out.append((placeholder, desc))
                continue
            # Reference to value_help definition
            if isinstance(item, str) and value_help is not None:
                ref = getattr(value_help, item, None)
                if isinstance(ref, (list, tuple)) and len(ref) >= 1:
                    ph = str(ref[0])
                    d = str(ref[1]) if len(ref) > 1 else ""
                    out.append((ph, d))
                    continue
            # Fallback: show as a literal token
            if isinstance(item, str):
                out.append((item, ""))
        return out

    @staticmethod
    def parse_input_tokens(raw_text: str) -> Tuple[List[str], bool, str]:
        """
        Parse raw input into tokens and identify typing context.

        Behavior:
        - Uses shlex to respect quotes; on parsing errors, falls back to str.split()
        - Detects if the input ends with a space (meaning the cursor is between tokens)
        - Separates the in-progress token as current_prefix when not between tokens

        Parameters:
        - raw_text: Entire prompt buffer text.

        Returns:
        - path_tokens: Complete tokens excluding the in-progress one when present
        - ends_with_space: True if raw_text ends with a space character
        - current_prefix: The partial token currently being typed ("" if between tokens)

        Examples:
        - "show ip "  -> (['show', 'ip'], True,  "")
        - "show ip r" -> (['show', 'ip'], False, "r")
        """
        try:
            parts = shlex.split(raw_text, posix=True)
            ends_with_space = raw_text.endswith(" ")
        except ValueError:
            parts = raw_text.split()
            ends_with_space = raw_text.endswith(" ")

        if not ends_with_space and parts:
            current_prefix = parts[-1]
            path_tokens = parts[:-1]
        else:
            current_prefix = ""
            path_tokens = parts
        return path_tokens, ends_with_space, current_prefix

    @staticmethod
    def filter_keys_by_prefix(keys: List[str], prefix: str) -> List[str]:
        """Filter completion candidates by the in-progress token prefix."""
        return [k for k in keys if k.startswith(prefix)] if prefix else keys

    @staticmethod
    def longest_common_prefix(strings: List[str]) -> str:
        """Compute textual LCP among strings for incremental completion."""
        if not strings:
            return ""
        first = min(strings)
        last = max(strings)
        limit = min(len(first), len(last))
        for i in range(limit):
            if first[i] != last[i]:
                return first[:i]
        return first[:limit]

    @staticmethod
    def ensure_single_trailing_space(buffer_text_before_cursor: str) -> bool:
        """True if pressing Tab should insert a trailing space after a unique match."""
        return not buffer_text_before_cursor.endswith(" ")

    # ---- Instance APIs over the tree ----
    def resolve_path(self, tokens: List[str]) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[str]]:
        """Walk tokens through the tree and return (node, consumed_path, error_token)."""
        """
        Walk tokens through the tree.
        For a tagNode, any token is accepted as a 'value' and consumed,
        but does not advance to a child node (we remain on the tagNode).
        Returns (resolved_node, consumed_path, error_token).
        """
        current_node = self.tree
        consumed_path: List[str] = []
        for token in tokens:
            child_map = CommandNavigator.get_children(current_node)
            if token in child_map:
                current_node = child_map[token]
                consumed_path.append(token)
                continue

            # SPECIAL: tagNode accepts any token as a value; consume it and stay.
            if CommandNavigator.is_tag_node(current_node):
                continue

            # Otherwise, unknown token at this level.
            return None, consumed_path, token

        return current_node, consumed_path, None

    def get_completion_context(self, raw_text: str) -> Tuple[Optional[Dict[str, Any]], str, bool, List[str]]:
        """
        Build a unified completion context used by autosuggest, Tab-complete and '?' help.

        This applies tagNode visibility rules at the source so all features behave consistently:
        - At a tagNode before its value is entered: only leaf children (e.g. 'summary') are exposed
        - After a tagNode value is entered: only non-leaf children (e.g. nested tagNodes like 'test') are exposed

        Parameters:
        - raw_text: The entire prompt buffer text up to the cursor

        Returns:
        - resolved_node: The node reached by walking complete tokens (or None on error)
        - current_prefix: The in-progress token prefix currently being typed ('' if between tokens)
        - ends_with_space: True if the buffer ends with space (meaning between tokens)
        - candidate_keys: Filtered, sorted child keys visible under current rules
        """
        path_tokens, ends_with_space, current_prefix = CommandNavigator.parse_input_tokens(raw_text)
        resolved_node, _, error_token = self.resolve_path(path_tokens)
        if error_token or resolved_node is None:
            return None, current_prefix, ends_with_space, []

        # Start from all child keys
        child_map = CommandNavigator.get_children(resolved_node)
        child_keys = sorted(child_map.keys())

        # Apply tagNode visibility rules consistently across autosuggest, Tab, and '?' help:
        # - If at a tagNode BEFORE a value is entered: show only leaf children (e.g., 'summary')
        # - If at a tagNode AFTER a value is entered: show only non-leaf children (e.g., nested tagNodes like 'test')
        if CommandNavigator.is_tag_node(resolved_node):
            # IMPORTANT: ignore the current incomplete prefix token when deciding if a value has been entered
            value_entered_for_node = self.tag_value_entered_for_node_with_tokens(resolved_node, path_tokens)
            if value_entered_for_node:
                child_keys = [k for k in child_keys if child_map.get(k, {}).get("type") != "leafNode"]
            else:
                child_keys = [k for k in child_keys if child_map.get(k, {}).get("type") == "leafNode"]

        if ends_with_space:
            # Between tokens: we don't filter by prefix.
            return resolved_node, "", True, child_keys

        # While typing a token: filter by prefix (keys only; tag value is never a key).
        return resolved_node, current_prefix, ends_with_space, CommandNavigator.filter_keys_by_prefix(child_keys, current_prefix)

    def execute_current_command(self, node: Dict[str, Any], raw_text: str) -> None:
        """
        Execute the command bound at the current node (print the command for now).

        Behavior:
        - If at a tagNode and its value was entered, prefer 'tagCommand' when present
        - Otherwise, run 'command' when present
        - If neither applies, emit an 'Incomplete command: <path>' message

        Parameters:
        - node: Node resolved from the current input tokens
        - raw_text: Full input line used to capture tag values for interpolation
        """
        child_map = CommandNavigator.get_children(node)
        tag_values = self.extract_tag_values(raw_text)
        # Prefer tagCommand when a tag value was entered for this node
        if CommandNavigator.is_tag_node(node) and self.tag_value_entered_for_node(node, raw_text) and "tagCommand" in node:
            command_to_execute = self._format_command_with_tags(node["tagCommand"], tag_values)
            print(command_to_execute)
            return
        # Otherwise, fallback to command if present
        if "command" in node:
            command_to_execute = self._format_command_with_tags(node.get("command", ""), tag_values)
            print(command_to_execute)
            return
        # No runnable command here; report incomplete command with the consumed path
        path_tokens, _, _ = CommandNavigator.parse_input_tokens(raw_text)
        _, consumed_path, _ = self.resolve_path(path_tokens)
        path_str = " ".join(consumed_path) if consumed_path else "(root)"
        print(f"Incomplete command: {path_str}")
        return

    def _format_command_with_tags(self, command: str, tag_values: Dict[str, str]) -> str:
        """Format a command string with captured tag values; leave unknown placeholders."""
        try:
            class _Default(dict):
                def __missing__(self, key):
                    return "{" + key + "}"
            return command.format_map(_Default(tag_values))
        except Exception:
            return command

    def extract_tag_values(self, raw_text: str) -> Dict[str, str]:
        """
        Capture entered tag values into a dictionary keyed by the tagNode name.

        Walk the tokens left-to-right and when we are sitting on a tagNode, the
        first subsequent token that is not a child key is treated as that tagNode's
        value. Values are not consumed as path segments and do not advance the node.

        Example:
        'show ip route 1.1.1.1/32 test abc' -> {'route': '1.1.1.1/32', 'test': 'abc'}
        """
        try:
            tokens = shlex.split(raw_text, posix=True)
        except ValueError:
            tokens = raw_text.split()

        current_node: Dict[str, Any] = self.tree
        current_key_name: Optional[str] = None
        values: Dict[str, str] = {}
        for token in tokens:
            child_map = CommandNavigator.get_children(current_node)
            if token in child_map:
                current_node = child_map[token]
                current_key_name = token
                continue
            if CommandNavigator.is_tag_node(current_node) and current_key_name:
                # First non-child token becomes the tag value for this tag node
                if current_key_name not in values:
                    values[current_key_name] = token
                # Stay on the same tag node to allow multiple value-bearing tokens downstream
                continue
            # Unknown token at this level; stop extracting
            break
        return values

    def tag_value_entered_for_node(self, node: Dict[str, Any], raw_text: str) -> bool:
        """
        Determine if the specific tagNode within the current input has already
        consumed a non-child token as its value.
        """
        """
        Return True only if the given tag node has already consumed a non-child token
        as its value in the current input line. Ignores values entered for other tag nodes.
        """
        try:
            tokens = shlex.split(raw_text, posix=True)
        except ValueError:
            tokens = raw_text.split()

        current_node: Dict[str, Any] = self.tree
        for token in tokens:
            child_map = CommandNavigator.get_children(current_node)
            if token in child_map:
                current_node = child_map[token]
                continue
            if CommandNavigator.is_tag_node(current_node):
                # If this is the node we are checking, then a value has been entered
                if current_node is node:
                    return True
                # Otherwise, this token is a value for some ancestor tag node; consume and continue
                # (stay on the same current_node)
                continue
            # Unknown token while not on a tag node – stop
            return False
        return False

    def tag_value_entered_for_node_with_tokens(self, node: Dict[str, Any], tokens: List[str]) -> bool:
        """
        Token-driven variant of tag value detection used while typing.

        This version accepts an explicit token list so the current incomplete
        prefix can be omitted when deciding whether a tag value has been entered.
        """
        """
        Same as tag_value_entered_for_node, but driven by a provided token list.
        Useful to ignore the current incomplete prefix while typing.
        """
        current_node: Dict[str, Any] = self.tree
        for token in tokens:
            child_map = CommandNavigator.get_children(current_node)
            if token in child_map:
                current_node = child_map[token]
                continue
            if CommandNavigator.is_tag_node(current_node):
                return current_node is node
            return False
        return False

    def tag_value_already_entered(self, raw_text: str) -> bool:
        """
        Return True if, while walking tokens (including the last token even
        without a trailing space), we remained on a tagNode and consumed at
        least one non-child token as its value.
        """
        try:
            tokens = shlex.split(raw_text, posix=True)
        except ValueError:
            tokens = raw_text.split()

        current_node: Dict[str, Any] = self.tree
        for token in tokens:
            child_map = CommandNavigator.get_children(current_node)
            if token in child_map:
                current_node = child_map[token]
                continue
            if CommandNavigator.is_tag_node(current_node):
                return True
            return False
        return False

# ---------------- Fish-style autosuggest ----------------

class TreeAutoSuggest(AutoSuggest):
    def __init__(self, navigator: CommandNavigator):
        self.nav = navigator

    def get_suggestion(self, buffer, document):
        resolved_node, current_prefix, ends_with_space, candidate_keys = self.nav.get_completion_context(
            document.text_before_cursor
        )
        if not candidate_keys or ends_with_space or current_prefix == "":
            return None

        if len(candidate_keys) == 1:
            only_key = candidate_keys[0]
            if only_key.startswith(current_prefix) and only_key != current_prefix:
                return Suggestion(only_key[len(current_prefix):] + " ")
            return None

        lcp_value = CommandNavigator.longest_common_prefix(candidate_keys)
        if lcp_value and lcp_value != current_prefix:
            return Suggestion(lcp_value[len(current_prefix):])
        return None


# ---------------- Keybindings encapsulated ----------------

class FishKeyBindings:
    """Encapsulates Tab and '?' behavior (no dropdowns; fish-style inline)."""

    def __init__(self, navigator: CommandNavigator, prompt_label: str = "router> "):
        self.nav = navigator
        self.prompt_label = prompt_label
        self.key_bindings = KeyBindings()
        self._bind_tab()
        self._bind_question_mark()

    def get(self) -> KeyBindings:
        return self.key_bindings

    def _bind_tab(self) -> None:
        @self.key_bindings.add("tab")
        def _(event):
            buffer = event.app.current_buffer
            suggestion = buffer.suggestion
            if suggestion:
                buffer.insert_text(suggestion.text)
                # Add one trailing space only when token is uniquely resolved
                _, current_prefix, ends_with_space, candidate_keys = self.nav.get_completion_context(
                    buffer.document.text_before_cursor
                )
                if (not ends_with_space) and len(candidate_keys) == 1 and candidate_keys[0] == current_prefix:
                    if CommandNavigator.ensure_single_trailing_space(buffer.document.text_before_cursor):
                        buffer.insert_text(" ")
                return

            raw_text = buffer.document.text_before_cursor
            resolved_node, current_prefix, ends_with_space, candidate_keys = self.nav.get_completion_context(raw_text)
            if not candidate_keys:
                return

            if ends_with_space:
                # Between tokens: insert LCP if any; otherwise do nothing.
                lcp_value = CommandNavigator.longest_common_prefix(candidate_keys)
                if lcp_value:
                    buffer.insert_text(lcp_value)
                return

            if len(candidate_keys) == 1:
                only_key = candidate_keys[0]
                if only_key.startswith(current_prefix):
                    buffer.insert_text(only_key[len(current_prefix):])
                    if CommandNavigator.ensure_single_trailing_space(buffer.document.text_before_cursor):
                        buffer.insert_text(" ")
                return

            lcp_value = CommandNavigator.longest_common_prefix(candidate_keys)
            if lcp_value and lcp_value != current_prefix:
                buffer.insert_text(lcp_value[len(current_prefix):])

    def _bind_question_mark(self) -> None:
        @self.key_bindings.add("?")
        def _(event):
            buffer = event.app.current_buffer
            raw_text = buffer.document.text_before_cursor

            resolved_node, current_prefix, ends_with_space, candidate_keys = self.nav.get_completion_context(raw_text)

            def _print_help():
                # Echo the prior prompt line first
                print(f"{self.prompt_label}{raw_text}")
                print("Possible completions:")

                if resolved_node is None:
                    print("(no suggestions)\n")
                    return

                child_map = CommandNavigator.get_children(resolved_node)
                rows: List[List[str]] = []

                # Show Enter as an available action when runnable (command or eligible tagCommand)
                runnable = False
                if "command" in resolved_node:
                    runnable = True
                if CommandNavigator.is_tag_node(resolved_node):
                    path_tokens, _, _ = CommandNavigator.parse_input_tokens(raw_text)
                    value_entered_for_node = self.nav.tag_value_entered_for_node_with_tokens(resolved_node, path_tokens)
                    if value_entered_for_node and "tagCommand" in resolved_node:
                        runnable = True
                if runnable:
                    rows.append(["<Enter>", "Execute the current command"])

                # 1) Add real child keys with tagNode-specific rules
                keys_to_show = candidate_keys
                if CommandNavigator.is_tag_node(resolved_node):
                    if self.nav.tag_value_entered_for_node(resolved_node, raw_text):
                        # After a value: show only non-leaf children (e.g., nested commands like 'test')
                        keys_to_show = [
                            key for key in candidate_keys
                            if child_map.get(key, {}).get("type") != "leafNode"
                        ]
                    else:
                        # Before a value: show only leaf children (e.g., 'summary')
                        keys_to_show = [
                            key for key in candidate_keys
                            if child_map.get(key, {}).get("type") == "leafNode"
                        ]
                for key in keys_to_show:
                    rows.append([key, child_map[key].get("description", "")])

                # 2) Add tag placeholders (if any and no value for this node yet)
                if CommandNavigator.is_tag_node(resolved_node) and not self.nav.tag_value_entered_for_node(resolved_node, raw_text):
                    for placeholder, desc in CommandNavigator.get_tag_placeholders(resolved_node):
                        rows.append([placeholder, desc])

                # 3) Do not add another Enter row; a single generic entry is sufficient

                if not rows:
                    print("(no suggestions)\n")
                    return

                # sort rows by the first column (the command/placeholder name)
                rows.sort(key=lambda r: r[0])

                print(tabulate(rows, tablefmt="plain"))
                print()

            run_in_terminal(_print_help) 