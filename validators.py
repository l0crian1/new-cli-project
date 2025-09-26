#!/usr/bin/env python3
def ipv4_address(value: str) -> bool:
    """Return True if value is a valid IPv4 address."""
    import ipaddress
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return (False, f"Must be a valid IPv4 address")
    
def ipv4_network(value: str) -> bool:
    """Return True if value is a valid IPv4 network."""
    import ipaddress
    try:
        return ipaddress.ip_network(value).version == 4
    except ValueError:
        return (False, f"Must be a valid IPv4 network")
