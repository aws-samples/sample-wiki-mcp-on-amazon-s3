# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every registered tool's descriptor has the shape MCP clients validate strictly.

Claude Desktop rejects the *entire* ``tools/list`` result if any single tool's
``outputSchema`` lacks a top-level ``"type": "object"`` (the spec requires it).
"""

from __future__ import annotations

from app.mcp.tools import registry


def test_every_schema_is_a_top_level_object() -> None:
    for name, tool in registry().items():
        descriptor = tool.descriptor()
        assert descriptor["inputSchema"].get("type") == "object", name
        if "outputSchema" in descriptor:
            assert descriptor["outputSchema"].get("type") == "object", name
