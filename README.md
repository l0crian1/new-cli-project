## Working with this CLI codebase

This guide explains the project layout, the end-to-end flow, how to extend the CLI (new commands, placeholders, behaviors), and the rules that drive autosuggestions, completions, help, and execution.

### Project structure

- `cli.py`
  - Entry point. Wires the interactive prompt with autosuggest and key bindings, and dispatches input to the navigator.
- `cli_common.py`
  - Core engine. Contains `CommandNavigator` (navigation, completion and execution), autosuggest and keybinding classes, and helper utilities.
- `op.json`
  - The operation tree (command taxonomy). You add/edit nodes here to define your CLI.
- `value_help.py`
  - Reusable value placeholders for tagNodes. Define placeholder/description pairs once and reference them by name.

### Runtime flow

1) Start
- `cli.py` loads `op.json` and constructs a `CommandNavigator`.

2) Prompt
- A `PromptSession` is created with `TreeAutoSuggest` (fish-style inline suggestions) and `FishKeyBindings` (Tab and `?` behaviors).

3) On each line
- The input is parsed into tokens and dispatched:
  - Navigation (walks tokens through `op.json`)
  - If valid: `CommandNavigator.execute_current_command(node, raw_text)`
  - If unknown token: prints `Unknown token 'X' at <path>`

4) Execution
- Currently prints the resolved command string; replace with real execution later (see “Switching to real execution”).

### Understanding the tree: nodes and behavior

- `type: "node"`
  - Regular container for nested commands.
- `type: "leafNode"`
  - A terminal command (e.g., `summary`). Appears as a child that can be selected; executed via its own `command` or parent context.
- `type: "tagNode"`
  - Requires a value token (e.g., an IP) before exposing certain children.
  - Supports:
    - `values`: list of placeholders (inline or references into `value_help.py`)
    - `command`: base command (before a value is entered)
    - `tagCommand`: command to run after a value is entered

Example excerpt:

```json
"route": {
  "description": "Show IP routes",
  "type": "tagNode",
  "values": ["ipv4net", "ipv4addr"],
  "command": "vtysh -c 'show ip route'",
  "tagCommand": "vtysh -c 'show ip route {route}'",
  "summary": {
    "description": "Summary of all routes",
    "type": "leafNode",
    "command": "vtysh -c 'show ip route summary'"
  },
  "test": {
    "description": "Nested tag node",
    "type": "tagNode",
    "tagCommand": "vtysh -c 'show ip route {route} {test}'"
  }
}
```

### Tag values and templating

- After a `tagNode` consumes a value, it is captured into a dict keyed by the `tagNode` name:
  - Input: `show ip route 1.1.1.1/32`
  - Captured: `{ "route": "1.1.1.1/32" }`
- On execution, placeholders like `{route}` and `{test}` are interpolated into `command`/`tagCommand`.
  - Unknown placeholders remain literal (e.g., `{unfilled}` stays as `{unfilled}`).

### Unified completion rules (affect autosuggest, Tab, and `?`)

Completion logic comes from `CommandNavigator.get_completion_context`, so all consumers are consistent.

- At a `tagNode` before a value is entered:
  - Only leaf children (e.g., `summary`) are visible.
  - Placeholders from `values` are shown under `?` help.
  - Non-leaf children (e.g., nested tagNodes like `test`) are hidden.
- At a `tagNode` after a value is entered:
  - Only non-leaf children (e.g., nested tagNodes like `test`) are visible.
  - Placeholders hidden.
- While typing, the in-progress prefix is ignored when deciding if a value was entered (typing `show ip route t` won’t suggest `test` unless `route` already has a value).

### What Enter executes

`?` shows `<Enter>` when runnable:
- Node has `command`, or
- Node is a `tagNode` with a value entered and has `tagCommand`.

Pressing Enter runs:
- `tagCommand` if a `tagNode` value is present and `tagCommand` exists; otherwise
- `command` if present; otherwise
- Prints `Incomplete command: <path>`.

### The `?` help

`?` prints:
- `<Enter>  Execute the current command` when runnable.
- Child commands per the visibility rules above.
- Value placeholders (only before a `tagNode` value is provided).

### The Tab key

- Expands suggestions based on the same candidates as autosuggest/`?`.
- Adds one trailing space after a unique completion.

### Unknown tokens and incomplete paths

- Unknown token: `Unknown token 'X' at <path>`
- Not runnable here: `Incomplete command: <path>`

### Adding new commands and nodes

1) Edit `op.json`

- Choose the correct type:
  - Containers: `node`
  - Terminals: `leafNode`
  - Needs argument: `tagNode`
- Provide `description` (used in help) and optionally `command`/`tagCommand`.
- For `tagNode` values, use either inline lists:

```json
"values": [["<placeholder>", "description"]]
```

or references into `value_help.py`:

```json
"values": ["ipv4addr", "ipv4net"]
```

2) Test interactions
- Before value: `?` shows placeholders + leaf children; no non-leaf children.
- After value: `?` shows only non-leaf children; Enter prefers `tagCommand`.

### Creating op-mode and conf-mode CLIs

- Reuse the same engine with different trees/prompts:
  - Create `op.json` and `conf.json`.
  - In separate entry points, load the desired JSON and set a distinct prompt (e.g., `op> ` vs `conf> `).

### Value placeholders library (`value_help.py`)

Define reusable placeholders:

```python
ipv4addr = ["<x.x.x.x>", "Show IP routes of specified IP address"]
ipv4net = ["<x.x.x.x/24>", "Show IP routes of specified IP address or prefix"]
```

Reference from `op.json`:

```json
"values": ["ipv4net", "ipv4addr"]
```

### Coding style and naming

- Use descriptive names; avoid single-letter identifiers for non-trivial scope.
- Non-trivial functions/classes include verbose docstrings (parameters, returns, examples, behaviors), especially for tagNode logic and completion rules.
- Simple helpers can be concise, but clarity is preferred.

### Switching to real execution

Currently, `CommandNavigator.execute_current_command(...)` prints the command string.

To execute for real:

```python
import subprocess
subprocess.run(command_to_execute, shell=True, check=False)
```

Consider:
- Dry-run toggle
- Timeouts
- Exit code/stderr reporting
- Windows vs POSIX nuances (quotes/escaping). For robust escaping, consider storing commands as arrays instead of single strings.

### Troubleshooting

- TagNode visibility:
  - Before value: placeholders + leaf children visible; non-leaf hidden
  - After value: non-leaf visible; placeholders hidden
  - Enter shows only if runnable (command or eligible tagCommand)
- Nested tagNodes don’t execute:
  - Ensure nested node has `tagCommand` and the value was captured (typing-time rules ignore the current incomplete token)
- Placeholder doesn’t show:
  - Verify `values` entries are inline lists or valid `value_help.py` references
- Unknown token:
  - Check the path and whether a `tagNode` is missing a value

### Quick examples

- Help before value at a tagNode:
  - Input: `show ip route ?`
  - Expect: placeholders + leaf children (e.g., `summary`) and Enter if runnable

- Enter a value and run:
  - Input: `show ip route 1.1.1.1/32` + Enter
  - Execution: `tagCommand` if present, else `command`

- Nested tagNode:
  - Input: `show ip route 1.1.1.1/32 test abc` + Enter
  - Execution: nested `tagCommand` with `{route}` and `{test}` substituted


