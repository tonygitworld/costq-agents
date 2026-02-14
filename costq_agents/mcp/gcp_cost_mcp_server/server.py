"""GCP Cost MCP Server (deprecated).

This module is no longer used. GCP MCP is now served via Gateway:
- costq-mcp-servers/src/gcp-billing-cost-management-mcp-server

This file is retained temporarily for reference and will be removed
once Gateway migration is verified.
"""

import logging

logger = logging.getLogger(__name__)


def main():
    """Deprecated entry point.

    GCP MCP is now served via Gateway. Do not start this server.
    """
    raise RuntimeError(
        "GCP MCP stdio server is deprecated. Use Gateway runtime: "
        "gcp-billing-cost-management-mcp-server"
    )


if __name__ == "__main__":
    main()
