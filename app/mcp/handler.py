# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda handler module for the MCP/data-plane function."""

from app.mcp.server import handle

__all__ = ["handle"]
