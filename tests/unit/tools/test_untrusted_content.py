# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every tool whose result can carry article text says that text is untrusted."""

from __future__ import annotations

from app.mcp.tools import registry
from app.mcp.tools._common import UNTRUSTED_CONTENT_NOTE

#: Bodies, frontmatter, titles, descriptions or snippets in a success result, or a
#: 409 ``current_body`` (update_article, archive_article, move_article).
RETURNS_ARTICLE_TEXT = {
    "search",
    "search_and_read",
    "list_folder",
    "read_article",
    "read_version",
    "update_article",
    "archive_article",
    "move_article",
}


def test_tools_returning_article_text_carry_the_note() -> None:
    marked = {
        name for name, tool in registry().items() if UNTRUSTED_CONTENT_NOTE in tool.description
    }
    # Equality, not subset: a new tool that returns article text has to be added here.
    assert marked == RETURNS_ARTICLE_TEXT


def test_the_note_reaches_the_descriptor() -> None:
    for name in RETURNS_ARTICLE_TEXT:
        assert UNTRUSTED_CONTENT_NOTE in registry()[name].descriptor()["description"]
