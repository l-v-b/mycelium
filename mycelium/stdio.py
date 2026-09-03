"""stdio transport entry point — `python -m mycelium.stdio`.

Claude Desktop (and other local MCP clients) launch an MCP server as a stdio
subprocess and speak JSON-RPC over its stdin/stdout. This is the command to
point `claude_desktop_config.json` at:

    {
      "mcpServers": {
        "mycelium": {
          "command": "C:\\\\path\\\\to\\\\python.exe",
          "args": ["-m", "mycelium.stdio"],
          "env": {"MYCELIUM_DATABASE_URL": "postgresql://postgres:changeme@localhost:5432/postgres"}
        }
      }
    }

CRITICAL: stdout is the wire. Nothing may print to stdout or the JSON-RPC stream
is corrupted and the client drops the connection. All diagnostics go to stderr.
"""
from __future__ import annotations

import sys

from mycelium.server import mcp


def main() -> None:
    print("mycelium: starting stdio transport", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
