"""Source adapters. Each implements SourceClient and calls register() on import.

Loaded via the allowlist mechanism in `federation/__init__.py.load_adapters`.
Adding a new external source = (1) drop a file here, (2) add the adapter
name to `enabled_adapters` in ~/.mycelium/config.json.
"""
