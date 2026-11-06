# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based fuzzing of the §10.2 path grammar and the ancestor walk.

Session policies interpolate a path that passed this grammar, and grant resolution
walks the path string. A path the grammar wrongly accepts, or an ancestor list that
reaches outside the path, is how a bug becomes an over-broad STS credential that IAM
cannot catch: the storage role's outer bound is all of ``a/*``. These run in the
default ``pytest`` run, so CI runs them on every change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.auth.grants import ancestors
from app.errors import ToolError
from app.mcp.tools._common import PATH_MAX_LENGTH, article_path, folder_path

# The autouse env fixture in tests/conftest.py is the same for every example.
_SETTINGS = settings(max_examples=500, suppress_health_check=[HealthCheck.function_scoped_fixture])

_LEGAL_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*")

# IAM wildcards and policy-variable syntax, separators, dot segments, whitespace and
# a newline ("$" matches before a trailing one), mixed with legal characters so that
# accepted paths come up often.
_HOSTILE = "az09._-/*?${}\\% \n\t\x00A"
_segment = st.from_regex(r"[a-z0-9][a-z0-9._-]{0,11}", fullmatch=True)
_hostile_segment = st.one_of(_segment, st.text(alphabet=_HOSTILE, max_size=8))
_hostile_path = st.builds(
    lambda segs, md, tail: "/" + "/".join(segs) + (".md" if md else "") + tail,
    st.lists(_hostile_segment, max_size=6),
    st.booleans(),
    st.sampled_from(["", "/", "\n", " ", "*"]),
)
_candidate = st.one_of(_hostile_path, st.text(alphabet=_HOSTILE, max_size=40), st.text(max_size=40))
_any_value = st.one_of(_candidate, st.none(), st.integers(), st.lists(st.text(max_size=5)))

_folder_segment = _segment.filter(lambda s: not s.endswith(".md"))
_folder = st.one_of(
    st.just("/"),
    st.lists(_folder_segment, min_size=1, max_size=6).map(lambda s: "/" + "/".join(s)),
)
_article = st.builds(
    lambda folders, name: "/" + "".join(f"{f}/" for f in folders) + f"{name}.md",
    st.lists(_folder_segment, max_size=6),
    _segment,
)


def _accepted(validate: Callable[[Any], str], value: Any) -> str | None:
    """The validated value, or ``None`` when refused. A refusal must be a 400."""
    try:
        return validate(value)
    except ToolError as exc:
        assert exc.status == 400, exc
        return None


def _assert_in_grammar(path: str, *, article: bool) -> None:
    """What the session policy and the ancestor walk rely on (§10.2)."""
    assert len(path) <= PATH_MAX_LENGTH
    assert path.startswith("/")
    if path == "/":
        assert not article
        return
    segments = path[1:].split("/")
    for seg in segments:
        # No IAM wildcard or variable, no empty, "." or ".." segment, no "_" lead
        # (``_listing.json``), no whitespace or newline.
        assert _LEGAL_SEGMENT.fullmatch(seg), (path, seg)
    for seg in segments[:-1]:
        assert not seg.endswith(".md"), path
    assert segments[-1].endswith(".md") is article, path


class TestPathGrammar:
    @_SETTINGS
    @given(_candidate)
    def test_article_path_accepts_only_the_grammar(self, value: str) -> None:
        out = _accepted(article_path, value)
        if out is not None:
            assert out == value
            _assert_in_grammar(out, article=True)

    @_SETTINGS
    @given(_candidate)
    def test_folder_path_accepts_only_the_grammar(self, value: str) -> None:
        out = _accepted(folder_path, value)
        if out is not None:
            assert out == value
            _assert_in_grammar(out, article=False)

    @_SETTINGS
    @given(_any_value)
    def test_any_input_is_accepted_or_a_400(self, value: Any) -> None:
        _accepted(article_path, value)
        _accepted(folder_path, value)

    @_SETTINGS
    @given(_article, _folder)
    def test_well_formed_paths_are_accepted(self, article: str, folder: str) -> None:
        # Positive control: a grammar that refused everything would pass the above.
        assert article_path(article) == article
        assert folder_path(folder) == folder

    @pytest.mark.parametrize("value", ["/x.md\n", "/racing/x.md\n", "/racing\n", "/\n"])
    def test_trailing_newline_is_refused(self, value: str) -> None:
        # "$" matches before a trailing newline; found by the properties above.
        validate = article_path if value.endswith(".md\n") else folder_path
        with pytest.raises(ToolError) as exc:
            validate(value)
        assert exc.value.status == 400


class TestAncestorWalk:
    @_SETTINGS
    @given(st.one_of(_article, _folder))
    def test_walk_is_every_prefix_at_a_segment_boundary(self, path: str) -> None:
        out = ancestors(path)
        assert out[0] == "/"
        assert out[-1] == path
        assert len(out) == (1 if path == "/" else path.count("/") + 1)
        for shorter, longer in zip(out, out[1:], strict=False):
            assert longer.startswith(shorter if shorter == "/" else f"{shorter}/")
            assert "/" not in longer[len(shorter) + (0 if shorter == "/" else 1) :]
        for node in out[:-1]:
            assert folder_path(node) == node

    @_SETTINGS
    @given(_folder, st.one_of(_article, _folder))
    def test_folder_reaches_only_paths_beneath_it(self, folder: str, path: str) -> None:
        # "/racing" must not reach "/racing2/x.md": the walk splits on "/", never on a
        # string prefix.
        beneath = folder == "/" or path == folder or path.startswith(f"{folder}/")
        assert (folder in ancestors(path)) is beneath

    @_SETTINGS
    @given(_candidate)
    def test_walk_never_leaves_the_input(self, value: str) -> None:
        try:
            out = ancestors(value)
        except ValueError:
            return
        trimmed = value.rstrip("/") or "/"
        assert out[-1] == trimmed
        for node in out:
            assert node == "/" or trimmed == node or trimmed.startswith(f"{node}/")
            if node != "/":
                assert not {"", ".", ".."} & set(node[1:].split("/")), node
