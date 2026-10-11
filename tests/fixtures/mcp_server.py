from mcp.server.fastmcp import FastMCP

server = FastMCP("regression-server")


@server.tool()
def echo(text: str) -> str:
    return "echo:" + text


if __name__ == "__main__":
    server.run(transport="stdio")
