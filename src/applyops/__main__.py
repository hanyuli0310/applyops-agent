"""Entry point for `python -m applyops`.

The project's entry point is the MCP server, not a UI: ApplyOps is "hands and
memory", and a harness supplies the reasoning. A console script
(`applyops-mcp`) is installed alongside this for harnesses that prefer to launch
an executable, but both paths arrive at the same stdio server.
"""

from __future__ import annotations


def main() -> None:
    from applyops.mcp.server import main as serve

    serve()


if __name__ == "__main__":
    main()
