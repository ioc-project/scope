#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The group starts the workers of its members, and every member's command can be loaded."""

import pytest

from eval_worker.cli import MEMBERS


@pytest.mark.parametrize("member", MEMBERS, ids=lambda member: member.name)
def test_member_command_loads(member):
    assert member.load().name  # a click command
