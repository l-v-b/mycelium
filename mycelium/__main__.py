from mycelium.config import HOST, PORT
from mycelium.server import mcp

mcp.run(transport="sse", host=HOST, port=PORT)
