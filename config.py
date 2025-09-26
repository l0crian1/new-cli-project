#!/usr/bin/env python3
"""Interactive config-mode CLI harness.

This CLI mirrors suggestions, completions, and `?` help behavior of cli.py,
but does not execute commands. It loads the configuration tree from config.json
and uses the same CommandNavigator engine for navigation.
"""

import json
from pathlib import Path
from typing import Dict, Any, List
import copy
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
 

from cli_common import (
    CommandNavigator,
    TreeAutoSuggest,
    FishKeyBindings,
)
import validators  # user-provided module with validation callables

JSON_PATH = Path("config.json")


def load_tree() -> Dict[str, Any]:
    with JSON_PATH.open("r", encoding="utf-8") as f:
        config_tree = json.load(f)
    # Wrap the loaded config under a base navigation key 'set'
    # so users navigate starting with: set ...
    return {
        "set": {
            "description": "Set the value of a parameter or create a new element",
            "type": "node",
            **config_tree,
        },
        "show": {
            "description": "Show the configuration",
            "type": "node",
            "candidate": {
                "description": "Show candidate (uncommitted) configuration",
                "type": "leafNode"
            }
        },
        "commit": {
            "description": "Commit the current set of changes",
            "type": "leafNode"
        },
        "compare": {
            "description": "Compare configuration revisions",
            "type": "leafNode"
        },
        "discard": {
            "description": "Discard uncommitted changes",
            "type": "leafNode"
        }
    }


def _set_nested_value(root: Dict[str, Any], keys: List[str], value: Any) -> None:
    """Create/update a nested mapping so that root[keys[0]][keys[1]]...[last] = value.

    If intermediate keys are missing or not dicts, they are created/overwritten as dicts.
    """
    current: Dict[str, Any] = root
    for key in keys[:-1]:
        node = current.get(key)
        if not isinstance(node, dict):
            node = {}
            current[key] = node
        current = node
    if keys:
        current[keys[-1]] = value


def _delete_nested_value(root: Dict[str, Any], keys: List[str]) -> bool:
    """Delete a nested value at root[keys...] if present. Do not prune parents.

    Returns True if something was deleted.
    """
    if not keys:
        return False
    key = keys[0]
    if key not in root:
        return False
    if len(keys) == 1:
        # Delete terminal key
        del root[key]
        return True
    child = root.get(key)
    if isinstance(child, dict):
        return _delete_nested_value(child, keys[1:])
    return False


def _convert_candidate_to_tree(node: Dict[str, Any], schema: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Convert a realized config subtree into a navigable tree with metadata.

    - Copies `description` from the schema when present for known keys
    - Marks containers as type=node and terminals as type=leafNode
    - For dynamic keys (e.g., tag values), no schema description exists; those
      entries are added without descriptions
    """
    tree: Dict[str, Any] = {}
    for key in sorted(node.keys()):
        value = node[key]
        schema_child = None
        if isinstance(schema, dict):
            if key in schema:
                schema_child = schema.get(key)
            elif schema.get("type") == "tagNode":
                # Carry tagNode schema through dynamic value keys (e.g., route 1.1.1.1/32)
                schema_child = schema
        description = None
        if isinstance(schema_child, dict):
            description = schema_child.get("description")

        if isinstance(value, dict) and value:
            entry: Dict[str, Any] = {"type": "node"}
            if description:
                entry["description"] = description
            entry.update(_convert_candidate_to_tree(value, schema_child))
            tree[key] = entry
        elif isinstance(value, list):
            # Represent lists as a node whose children are the list items
            entry_list: Dict[str, Any] = {"type": "node"}
            if description:
                entry_list["description"] = description
            for item in value:
                # Child keys are the string form of the list items
                entry_list[str(item)] = {"type": "leafNode"}
            tree[key] = entry_list
        else:
            entry = {"type": "leafNode"}
            if description:
                entry["description"] = description
            tree[key] = entry
    return tree


def _build_delete_tree(candidate_config: Dict[str, Any], schema_root: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "delete": {
            "description": "Delete a configuration element",
            "type": "node",
            **_convert_candidate_to_tree(candidate_config, schema_root),
        }
    }


def _build_show_tree(committed_root: Dict[str, Any]) -> Dict[str, Any]:
    # Reuse the same converter to represent the committed structure under 'show'
    return {
        "show": {
            "description": "Show the configuration",
            "type": "node",
            "candidate": {
                "description": "Show candidate (uncommitted) configuration",
                "type": "leafNode",
            },
            **_convert_candidate_to_tree(committed_root),
        }
    }


def _get_nested_value(root: Dict[str, Any], keys: List[str]) -> Any:
    current: Any = root
    for key in keys:
        if not isinstance(current, dict):
            return {}
        if key not in current:
            return {}
        current = current[key]
    return current


def main():
    """Start the interactive config-mode CLI loop (no command execution)."""
    base_tree = load_tree()
    committed_path = Path("committed_config.json")
    try:
        with committed_path.open("r", encoding="utf-8") as f:
            committed_base: Dict[str, Any] = json.load(f)
    except FileNotFoundError:
        committed_base = {}
    # Working view of committed, subject to uncommitted deletes
    committed_working: Dict[str, Any] = copy.deepcopy(committed_base)
    candidate_config: Dict[str, Any] = {}

    def _merge_dicts(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        # Keys from both
        for key in set(left.keys()) | set(right.keys()):
            lv = left.get(key)
            rv = right.get(key)
            if isinstance(lv, dict) and isinstance(rv, dict):
                merged[key] = _merge_dicts(lv, rv)
            elif rv is not None:
                merged[key] = rv
            else:
                merged[key] = lv
        return merged

    def _emit_removed_leaves(prefix: List[str], node: Any, out_lines: List[str]) -> None:
        # Walk a subtree and emit a '-' line for every leaf-like option
        if isinstance(node, dict):
            for k in sorted(node.keys()):
                v = node[k]
                if isinstance(v, dict):
                    if v:
                        _emit_removed_leaves(prefix + [k], v, out_lines)
                    else:
                        # Empty container removed
                        out_lines.append(f"- {' '.join(prefix + [k])}")
                else:
                    if v is True:
                        out_lines.append(f"- {' '.join(prefix + [k])}")
                    else:
                        out_lines.append(f"- {' '.join(prefix + [k, str(v)])}")
        else:
            # Scalar leaf
            out_lines.append(f"- {' '.join(prefix + [str(node)])}")

    def _emit_added_leaves(prefix: List[str], node: Any, out_lines: List[str]) -> None:
        # Walk a subtree and emit a '+' line for every leaf-like option, including values
        if isinstance(node, dict):
            if not node:
                out_lines.append(f"+ {' '.join(prefix)}")
                return
            for k in sorted(node.keys()):
                v = node[k]
                if isinstance(v, (dict, list)):
                    _emit_added_leaves(prefix + [k], v, out_lines)
                else:
                    if v is True:
                        out_lines.append(f"+ {' '.join(prefix + [k])}")
                    else:
                        out_lines.append(f"+ {' '.join(prefix + [k, str(v)])}")
            return
        if isinstance(node, list):
            if not node:
                out_lines.append(f"+ {' '.join(prefix)}")
                return
            for item in node:
                if item is True:
                    out_lines.append(f"+ {' '.join(prefix)}")
                else:
                    out_lines.append(f"+ {' '.join(prefix + [str(item)])}")
            return
        # Scalar leaf
        if node is True:
            out_lines.append(f"+ {' '.join(prefix)}")
        else:
            out_lines.append(f"+ {' '.join(prefix + [str(node)])}")

    def _to_removed_format(node: Any) -> Any:
        if isinstance(node, dict):
            out: Dict[str, Any] = {}
            for k in sorted(node.keys()):
                out[k] = _to_removed_format(node[k])
            return out
        # For leaves, keep original: True for flags, scalar for scalar values
        return node

    def _collect_removed(a: Any, b: Any) -> Dict[str, Any]:
        """Build a tree of items present in a but removed in b."""
        removed: Dict[str, Any] = {}
        if isinstance(a, dict):
            if not isinstance(b, dict):
                # Entire subtree removed
                return _to_removed_format(a) if isinstance(_to_removed_format(a), dict) else {}
            for k in a.keys():
                if k not in b:
                    removed[k] = _to_removed_format(a[k])
                else:
                    sub = _collect_removed(a[k], b[k])
                    if sub:
                        removed[k] = sub
            return removed
        # a is a leaf; if b is missing, signal removal by returning as a mapping at caller
        return {}

    def _diff(prefix: List[str], a: Any, b: Any, out_lines: List[str]) -> None:
        # a: committed, b: candidate
        if isinstance(a, dict) and isinstance(b, dict):
            keys = set(a.keys()) | set(b.keys())
            for k in sorted(keys):
                _diff(prefix + [k], a.get(k), b.get(k), out_lines)
            return
        if isinstance(a, dict) and not isinstance(b, dict):
            # dict replaced with leaf or removed entirely
            if b is None:
                # Entire subtree removed -> emit every leaf under this subtree
                _emit_removed_leaves(prefix, a, out_lines)
            else:
                # Flatten removals of children then add new leaf
                for k in sorted(a.keys()):
                    _diff(prefix + [k], a[k], None, out_lines)
                _diff(prefix, None, b, out_lines)
            return
        if not isinstance(a, dict) and isinstance(b, dict):
            # leaf replaced with dict (treat as add of all children)
            if a is None:
                # Entire subtree added -> emit every leaf under this subtree
                _emit_added_leaves(prefix, b, out_lines)
            else:
                for k in sorted(b.keys()):
                    _diff(prefix + [k], None, b[k], out_lines)
            return
        # Leaves or None
        if a is None and b is not None:
            if isinstance(b, list):
                for item in b:
                    if item is True:
                        out_lines.append(f"+ {' '.join(prefix)}")
                    else:
                        out_lines.append(f"+ {' '.join(prefix + [str(item)])}")
            elif b is True:
                out_lines.append(f"+ {' '.join(prefix)}")
            else:
                out_lines.append(f"+ {' '.join(prefix + [str(b)])}")
            return
        if a is not None and b is None:
            out_lines.append(f"- {' '.join(prefix)}")
            return
        if a is not None and b is not None and not isinstance(a, dict) and not isinstance(b, dict) and a != b:
            # Leaf value changed; emit both old and new for clarity and to trigger script collection
            def _emit_leaf(sign: str, val: Any) -> None:
                if isinstance(val, list):
                    for item in val:
                        out_lines.append(f"{sign} {' '.join(prefix + [str(item)])}")
                elif val is True:
                    out_lines.append(f"{sign} {' '.join(prefix)}")
                else:
                    out_lines.append(f"{sign} {' '.join(prefix + [str(val)])}")
            _emit_leaf("-", a)
            _emit_leaf("+", b)
            return

    

    # Navigator includes dynamic delete tree from merged configs
    merged_for_delete = _merge_dicts(committed_working, candidate_config)
    schema_root = base_tree.get("set", {})
    navigator = CommandNavigator({**base_tree, **_build_delete_tree(merged_for_delete, schema_root), **_build_show_tree(committed_base)})

    session = PromptSession(
        auto_suggest=TreeAutoSuggest(navigator),
        key_bindings=FishKeyBindings(navigator, prompt_label="config> ").get(),
        history=FileHistory(".config_history"),
    )

    # --- Helpers to modularize the interactive loop ---
    def _refresh_dynamic_tree() -> None:
        merged = _merge_dicts(committed_working, candidate_config)
        schema = base_tree.get("set", {})
        navigator.tree = {**base_tree, **_build_delete_tree(merged, schema), **_build_show_tree(committed_base)}

    def _call_validator(func: Any, value: Any) -> tuple[bool, str]:
        try:
            result = func(value)
        except Exception as exc:
            return False, f"validator exception: {exc}"
        if isinstance(result, tuple):
            ok = bool(result[0])
            msg = str(result[1]) if len(result) > 1 else ""
            return ok, msg
        return (bool(result), "" if result else "validation failed")

    # (no other validation helpers)

    def _compute_is_complete(head: str, tokens: List[str], resolved_node: Dict[str, Any] | None, line: str) -> bool:
        if head == "delete" and len(tokens) > 1:
            return True
        if resolved_node is None:
            return False
        if CommandNavigator.is_tag_node(resolved_node):
            return navigator.tag_value_entered_for_node(resolved_node, line)
        return resolved_node.get("type") == "leafNode"

    def _validate_set_line(text: str) -> tuple[bool, str]:
        tokens = text.split()
        if not tokens or tokens[0].lower() != "set":
            return True, ""
        _, consumed_path, error_token = navigator.resolve_path(tokens)
        if error_token is not None:
            return True, ""
        path_keys = [p for p in consumed_path if p != "set"]
        if not path_keys:
            return True, ""
        tag_values = navigator.extract_tag_values(text)
        schema_cursor: Any = base_tree.get("set", {})
        for seg in path_keys:
            next_schema = schema_cursor.get(seg)
            schema_cursor = next_schema
            if not next_schema or next_schema.get("type") != "tagNode":
                continue
            names = next_schema.get("validator")
            if not names:
                continue
            validator_names = [names] if isinstance(names, str) else [str(n) for n in names] if isinstance(names, list) else []
            v_value = tag_values.get(seg)
            if v_value is None:
                continue
            for name in validator_names:
                func = getattr(validators, name, None)
                if not callable(func):
                    return False, f"validator not found: {name}"
                ok, msg = _call_validator(func, v_value)
                if not ok:
                    return False, (msg or "Validation failed")
        return True, ""

    def _apply_set(line: str, consumed: List[str]) -> None:
        path_keys = [p for p in consumed if p != "set"]
        key_parts: List[str] = []
        terminal_value: Any = True
        tag_values = navigator.extract_tag_values(line)
        schema_cursor: Any = base_tree.get("set", {})
        terminal_schema: Dict[str, Any] | None = None
        for index, seg in enumerate(path_keys):
            key_parts.append(seg)
            next_schema = schema_cursor.get(seg)
            if next_schema and next_schema.get("type") == "tagNode":
                v = tag_values.get(seg)
                if v is not None:
                    if next_schema.get("valueMode") == "scalar":
                        if index == len(path_keys) - 1:
                            terminal_value = v
                    else:
                        key_parts.append(v)
            schema_cursor = next_schema
            terminal_schema = next_schema if index == len(path_keys) - 1 else terminal_schema
        if key_parts:
            # If 'multi' is true, persist value as a list (seed from committed when absent in candidate)
            is_multi = isinstance(terminal_schema, dict) and terminal_schema.get("multi") is True
            if is_multi:
                existing = _get_nested_value(candidate_config, key_parts)
                if existing == {}:
                    existing = None
                if existing is None:
                    existing = _get_nested_value(committed_working, key_parts)
                    if existing == {}:
                        existing = None
                values = existing if isinstance(existing, list) else ([] if existing is None else [existing])
                if terminal_value is not True and terminal_value not in values:
                    values.append(terminal_value)
                _set_nested_value(candidate_config, key_parts, values)
            else:
                _set_nested_value(candidate_config, key_parts, terminal_value)

    def _apply_delete(tokens: List[str], resolved_node: Dict[str, Any]) -> None:
        key_parts = tokens[1:]
        if CommandNavigator.is_tag_node(resolved_node) and resolved_node.get("valueMode") == "scalar":
            if len(key_parts) >= 2:
                key_parts = key_parts[:-1]
        # Support deletion of a specific item within a list leaf: ... next-hop <value>
        if len(key_parts) >= 2:
            base_path = key_parts[:-1]
            item_token = key_parts[-1]
            existing_candidate = _get_nested_value(candidate_config, base_path)
            if existing_candidate == {}:
                existing_candidate = None
            existing_committed = _get_nested_value(committed_working, base_path)
            if existing_committed == {}:
                existing_committed = None
            current_values: List[Any] | None = None
            if isinstance(existing_candidate, list):
                current_values = list(existing_candidate)
            elif isinstance(existing_committed, list):
                current_values = list(existing_committed)
            if isinstance(current_values, list) and item_token in current_values:
                new_values = [v for v in current_values if v != item_token]
                if new_values:
                    _set_nested_value(candidate_config, base_path, new_values)
                    _set_nested_value(committed_working, base_path, new_values)
                else:
                    _delete_nested_value(candidate_config, base_path)
                    _delete_nested_value(committed_working, base_path)
                return
        if key_parts:
            _delete_nested_value(candidate_config, key_parts)
            _delete_nested_value(committed_working, key_parts)

    def _process_input_line(line: str) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], bool]:
        nonlocal committed_base, committed_working, candidate_config
        tokens = line.split()
        if not tokens:
            return committed_base, committed_working, candidate_config, False
        head = tokens[0].lower()

        if head == "exit":
            print("Exiting...")
            return committed_base, committed_working, candidate_config, True

        if head == "commit":
            new_committed = _merge_dicts(committed_working, candidate_config)
            out_path = Path("committed_config.json")
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(new_committed, f, indent=2, ensure_ascii=False)
            # Build script list only for sections that changed (+/-)
            lines: List[str] = []
            _diff([], committed_base, new_committed, lines)
            scripts: List[str] = []

            schema_root = base_tree.get("set", {})

            def _find_script_for_path(path_tokens: List[str]) -> str | None:
                cursor = schema_root
                script_found: str | None = None
                i = 0
                while i < len(path_tokens) and isinstance(cursor, dict):
                    tok = path_tokens[i]
                    child = cursor.get(tok)
                    if isinstance(child, dict):
                        cursor = child
                        if "script" in cursor and cursor.get("script"):
                            script_found = str(cursor.get("script"))
                        i += 1
                        continue
                    # Handle tagNode dynamics
                    if cursor.get("type") == "tagNode":
                        value_mode = cursor.get("valueMode")
                        if value_mode == "scalar":
                            # Current token is the scalar value; stop descending further
                            break
                        # Non-scalar: token is a dynamic key under this tag node; keep cursor at same schema
                        i += 1
                        continue
                    break
                return script_found

            for ln in lines:
                ln = ln.strip()
                if not ln or (ln[0] not in "+-"):
                    continue
                parts = ln.split()
                if len(parts) < 2:
                    continue
                tokens = parts[1:]
                script_name = _find_script_for_path(tokens)
                if script_name:
                    scripts.append(script_name)
            committed_base = new_committed
            committed_working = copy.deepcopy(new_committed)
            candidate_config = {}
            print(f"Saved committed config to {out_path}")
            if scripts:
                # De-duplicate preserving order
                seen = set()
                ordered: List[str] = []
                for s in scripts:
                    if s not in seen:
                        seen.add(s)
                        ordered.append(s)
                # Execute scripts sequentially
                from utils.process import cmd
                for s in ordered:
                    script_path = Path(s)
                    if not script_path.exists():
                        print(f"Script not found: {s}")
                        continue
                    try:
                        result = cmd(f"{sys.executable} {script_path}")
                        if result.stdout:
                            print(result.stdout, end="")
                        if result.stderr:
                            print(result.stderr, end="")
                        if result.returncode != 0:
                            print(f"{s} exited with code {result.returncode}")
                    except Exception as exc:
                        print(f"Error running {s}: {exc}")
            return committed_base, committed_working, candidate_config, False

        if head == "show":
            if len(tokens) == 1:
                print(json.dumps(committed_base, indent=2, ensure_ascii=False))
            elif len(tokens) >= 2 and tokens[1].lower() == "candidate":
                deletions_tree = _collect_removed(committed_base, committed_working)
                additions_tree = candidate_config
                print(json.dumps({"add": additions_tree, "delete": deletions_tree}, indent=2, ensure_ascii=False))
            else:
                subtree = _get_nested_value(committed_base, tokens[1:])
                print(json.dumps(subtree, indent=2, ensure_ascii=False))
            return committed_base, committed_working, candidate_config, False

        if head == "compare":
            lines: List[str] = []
            working_view = _merge_dicts(committed_working, candidate_config)
            _diff([], committed_base, working_view, lines)
            if not lines:
                print("No differences.")
            else:
                print("\n".join(lines))
            return committed_base, committed_working, candidate_config, False

        if head == "discard":
            candidate_config = {}
            print("Uncommitted changes discarded.")
            return committed_base, committed_working, candidate_config, False

        resolved_node, consumed_path, error_token = navigator.resolve_path(tokens)
        if error_token is not None or resolved_node is None:
            # Special-case: support deleting a specific item from a list leaf
            if head == "delete" and error_token is not None and consumed_path:
                base_path = [p for p in consumed_path if p != "delete"]
                existing_candidate = _get_nested_value(candidate_config, base_path)
                if existing_candidate == {}:
                    existing_candidate = None
                existing_committed = _get_nested_value(committed_working, base_path)
                if existing_committed == {}:
                    existing_committed = None
                current_values: List[Any] | None = None
                if isinstance(existing_candidate, list):
                    current_values = list(existing_candidate)
                elif isinstance(existing_committed, list):
                    current_values = list(existing_committed)
                if isinstance(current_values, list) and error_token in current_values:
                    current_values = [v for v in current_values if v != error_token]
                    if current_values:
                        _set_nested_value(committed_working, base_path, current_values)
                        _set_nested_value(candidate_config, base_path, current_values)
                    else:
                        _delete_nested_value(candidate_config, base_path)
                        _delete_nested_value(committed_working, base_path)
                    return committed_base, committed_working, candidate_config, False
            path_str = " ".join(consumed_path) if consumed_path else "(root)"
            print(f"Unknown token '{error_token}' at {path_str}")
            return committed_base, committed_working, candidate_config, False

        is_complete = _compute_is_complete(head, tokens, resolved_node, line)
        if not is_complete:
            path_tokens, _, _ = CommandNavigator.parse_input_tokens(line)
            _, consumed, _ = navigator.resolve_path(path_tokens)
            path_str = " ".join(consumed) if consumed else "(root)"
            print(f"Incomplete command: {path_str}")
            return committed_base, committed_working, candidate_config, False

        path_tokens, _, _ = CommandNavigator.parse_input_tokens(line)
        _, consumed, _ = navigator.resolve_path(path_tokens)
        if head == "set":
            _apply_set(line, consumed)
        elif head == "delete":
            _apply_delete(tokens, resolved_node)

        return committed_base, committed_working, candidate_config, False

    next_default: str | None = None
    while True:
        try:
            # Refresh dynamic delete tree before each prompt using merged configs
            _refresh_dynamic_tree()
            user_line = session.prompt("config> ", default=(next_default or ""))
            next_default = None
        except KeyboardInterrupt:
            # Clear current line default and return to a fresh prompt
            next_default = None
            continue
        except EOFError:
            print("\nExiting CLI...")
            break

        for raw_line in user_line.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Validate set lines before processing; on failure, re-prompt with same input
            ok, err = _validate_set_line(line)
            if not ok:
                print(err)
                next_default = line
                break
            committed_base, committed_working, candidate_config, should_exit = _process_input_line(line)
            if should_exit:
                return


if __name__ == "__main__":
    main()


