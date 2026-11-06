# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda handler module for the web function."""

from app.web.app import handle

__all__ = ["handle"]
