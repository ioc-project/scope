#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import pytest

from cognitive_challenge_worker.challenges import all_challenges, select_challenges


def test_the_whole_taxonomy_is_shipped_and_complete():
    challenges = all_challenges()

    assert len(challenges) == 20
    for code, challenge in challenges.items():
        assert challenge["summary"] and challenge["signature"] and challenge["heuristic"], code
        assert challenge["locus"] in {
            "Individual",
            "Dyadic",
            "Collective",
            "Individual → Dyadic",
            "Individual → Collective",
        }


def test_codes_are_selected_in_order_ignoring_case():
    assert [c["code"] for c in select_challenges(["cc-3", "IR-1", " ic-2 "])] == ["CC-3", "IR-1", "IC-2"]


def test_a_challenge_selected_twice_is_selected_once():
    assert [c["code"] for c in select_challenges(["IR-1", "ir-1"])] == ["IR-1"]


def test_an_unknown_code_is_refused_with_the_available_ones():
    with pytest.raises(ValueError, match="NOPE.*Available: IR-1"):
        select_challenges(["IR-1", "NOPE"])


def test_nothing_selected_is_nothing():
    assert select_challenges([]) == []
