from mycelium.config import HOST, METRICS_PORT, PORT
from mycelium.metrics import start_exporter
from mycelium.server import mcp

# Prometheus exporter on a side port (no-op unless MYCELIUM_METRICS_PORT is set
# and the [metrics] extra is installed). Kept off the MCP transport port.
start_exporter(METRICS_PORT, addr=HOST)

mcp.run(transport="sse", host=HOST, port=PORT)
