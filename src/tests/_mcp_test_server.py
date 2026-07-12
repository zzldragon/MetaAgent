"""Tiny MCP server over stdio, used by _verify_mcp.py."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-mcp")


@mcp.tool()
def add_numbers(a: int, b: int) -> str:
    """Add two integers and return the sum."""
    return str(a + b)


@mcp.tool()
def shout(text: str) -> str:
    """Return the text in upper case."""
    return text.upper()


if __name__ == "__main__":
    mcp.run(transport="stdio")
