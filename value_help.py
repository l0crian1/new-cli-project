"""Reusable value placeholders for tagNodes.

Each symbol is a two-item list of [placeholder, description].
These can be referenced from op.json under a tagNode's "values" via string key.
"""

ipv4addr = ["<x.x.x.x>", "Show IP routes of specified IP address"]
ipv4net = ["<x.x.x.x/24>", "Show IP routes of specified IP address or prefix"]