"""The allocator's decisions: which slot gets which review, fix round or new issue."""

from __future__ import annotations

import random
from typing import Any

import pytest
from fleet_allocate import (
    Assignment,
    Config,
    Issue,
    Plan,
    Pull,
    Slot,
    State,
    classify_ci,
    has_done_when,
    in_flight_slots,
    parse_config,
    plan,
)

DONE = "## Problem\n\nx\n\n## Done when\n\n- [ ] the thing works\n- [ ] Lint passes.\n"


def config(**kw: object) -> Config:
    base: dict[str, object] = {
        "repo": "o/r",
        "slots": ["agent01", "agent02", "agent03"],
        "review_slots": ["review01"],
        "lanes": {"feature": 3, "fix": 2, "tooling": 1},
    }
    base.update(kw)
    return parse_config(base)


def issue(
    n: int,
    lane: str = "feature",
    body: str = DONE,
    labels: tuple[str, ...] | None = None,
    assigned: bool = False,
) -> Issue:
    tags = labels if labels is not None else ("agent-ready", f"lane:{lane}")
    return Issue(n, tags, body, f"2026-11-{n:02d}", assigned)


def pull(
    n: int,
    issue_no: int,
    ci: str = "pass",
    verdict: str | None = None,
    conflicting: bool = False,
    draft: bool = False,
    sha: str = "a" * 40,
) -> Pull:
    return Pull(
        number=n,
        head_ref=f"issue/{issue_no}",
        head_sha=sha,
        draft=draft,
        conflicting=conflicting,
        ci=ci,
        verdict=verdict,
        created_at=f"2026-11-{n:02d}",
    )


def free(cfg: Config) -> list[Slot]:
    return [Slot(name, "author", "free", None) for name in cfg.author_slots] + [
        Slot(name, "review", "free", None) for name in cfg.review_slots
    ]


def assigned(result_plan: Plan) -> dict[str, str]:
    return {slot: a.text() for slot, a in result_plan.assignments}


# --- configuration ------------------------------------------------------------------------------


def test_config_rejects_bad_slots_lanes_and_shares() -> None:
    bad: dict[str, Any]
    for bad in (
        {"slots": ["root"]},
        {"slots": ["agent01", "agent01"]},
        {"review_slots": ["agent02"]},
        {"slots": ["review01"]},
        {"lanes": {}},
        {"lanes": {"feature": 0}},
        {"lanes": {"Feature X": 1}},
        {"in_flight_share": 1.5},
        {"repo": "not a repo"},
        {"max_runs_per_issue": 0},
    ):
        with pytest.raises(ValueError):
            config(**bad)


def test_in_flight_is_two_thirds_of_the_author_slots_rounded() -> None:
    assert [in_flight_slots(n, 0.67) for n in (1, 2, 3, 6, 12)] == [1, 1, 2, 4, 8]


# --- reading issues and checks --------------------------------------------------------------------


def test_done_when_needs_a_checkbox_with_text_under_its_heading() -> None:
    assert has_done_when(DONE)
    assert has_done_when("### done when\r\n* [ ] works\r\n")
    assert not has_done_when("## Done when\n\n- [ ]\n")
    assert not has_done_when("## Problem\n- [ ] works\n## Done when\n\nnothing yet\n")
    assert not has_done_when("Done when: - [ ] works")
    assert not has_done_when("")


def test_ci_is_failed_running_or_passed_ignoring_the_verdict_check() -> None:
    ok = {"name": "backend", "status": "COMPLETED", "conclusion": "SUCCESS"}
    assert classify_ci([ok]) == "pass"
    assert (
        classify_ci([ok, {"name": "e2e", "status": "COMPLETED", "conclusion": "SKIPPED"}]) == "pass"
    )
    assert (
        classify_ci([ok, {"name": "e2e", "status": "IN_PROGRESS", "conclusion": ""}]) == "running"
    )
    assert (
        classify_ci([ok, {"name": "e2e", "status": "COMPLETED", "conclusion": "FAILURE"}]) == "fail"
    )
    assert classify_ci([ok, {"context": "legacy", "state": "PENDING"}]) == "running"
    assert classify_ci([ok, {"context": "legacy", "state": "ERROR"}]) == "fail"
    verdict = {"name": "reviewer-verdict", "status": "COMPLETED", "conclusion": "FAILURE"}
    assert classify_ci([ok, verdict]) == "pass"
    assert classify_ci([]) == "running"  # nothing registered yet: wait rather than review
    assert classify_ci([verdict]) == "running"


# --- what each kind of PR gets ------------------------------------------------------------------


def test_a_green_pr_without_a_verdict_goes_to_a_review_slot() -> None:
    cfg = config()
    result = plan(cfg, free(cfg), [], [pull(40, 12)], State(), random.Random(1))
    assert assigned(result)["review01"] == "review 40"
    assert not any(a.kind != "review" for _, a in result.assignments)


def test_a_red_pr_is_never_reviewed_and_gets_a_fix_round() -> None:
    cfg = config()
    result = plan(cfg, free(cfg), [], [pull(40, 12, ci="fail")], State(), random.Random(1))
    assert "review01" not in assigned(result)
    assert list(assigned(result).values()) == ["fix 12"]


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"verdict": "request_changes"}, "fix 12"),
        ({"conflicting": True}, "fix 12"),
        ({"verdict": "approve"}, None),
        ({"ci": "running"}, None),
        ({"draft": True}, None),
    ],
)
def test_what_a_pr_gets(kw: dict[str, Any], expected: str | None) -> None:
    cfg = config()
    result = plan(cfg, free(cfg), [], [pull(40, 12, **kw)], State(), random.Random(1))
    assert list(assigned(result).values()) == ([expected] if expected else [])


def test_a_pr_not_on_an_issue_branch_is_left_to_people() -> None:
    cfg = config()
    human = Pull(41, "feat/thing", "b" * 40, False, False, "fail", None, "2026-11-01")
    result = plan(cfg, free(cfg), [], [human], State(), random.Random(1))
    assert result.assignments == []


def test_the_same_head_is_never_reviewed_twice() -> None:
    cfg = config()
    state = State(reviewed={40: ["a" * 40]})
    result = plan(cfg, free(cfg), [], [pull(40, 12)], state, random.Random(1))
    assert result.assignments == []
    assert any("already reviewed" in n for n in result.notes)
    newer = plan(cfg, free(cfg), [], [pull(40, 12, sha="c" * 40)], state, random.Random(1))
    assert assigned(newer) == {"review01": "review 40"}


def test_a_pr_past_the_run_cap_is_left_for_a_person() -> None:
    cfg = config(max_runs_per_issue=3)
    state = State(runs={12: 3})
    result = plan(cfg, free(cfg), [], [pull(40, 12, ci="fail")], state, random.Random(1))
    assert result.assignments == []
    assert any("#40" in n and "person" in n for n in result.notes)


def test_reviews_go_oldest_first_and_only_to_review_slots() -> None:
    cfg = config(review_slots=["review01", "review02"])
    pulls = [pull(43, 15), pull(41, 13), pull(42, 14)]
    result = plan(cfg, free(cfg), [], pulls, State(), random.Random(1))
    assert assigned(result) == {"review01": "review 41", "review02": "review 42"}


# --- new issues ---------------------------------------------------------------------------------


def test_new_issues_fill_author_slots_oldest_first_within_a_lane() -> None:
    cfg = config(lanes={"feature": 1})
    result = plan(cfg, free(cfg), [issue(9), issue(3), issue(5)], [], State(), random.Random(1))
    assert sorted(assigned(result).values()) == ["issue 3", "issue 5", "issue 9"]


@pytest.mark.parametrize(
    "bad",
    [
        issue(3, labels=("lane:feature",)),  # not agent-ready
        issue(3, labels=("agent-ready",)),  # no lane
        issue(3, labels=("agent-ready", "lane:unknown")),
        issue(3, body="## Done when\n\n- [ ]\n"),  # template not filled in
        issue(3, assigned=True),  # a person has it
    ],
)
def test_an_issue_that_is_not_ready_is_skipped(bad: Issue) -> None:
    cfg = config()
    result = plan(cfg, free(cfg), [bad], [], State(), random.Random(1))
    assert result.assignments == []


def test_an_issue_with_an_open_pr_is_in_flight_not_new() -> None:
    cfg = config()
    result = plan(
        cfg, free(cfg), [issue(12)], [pull(40, 12, ci="running")], State(), random.Random(1)
    )
    assert result.assignments == []


def test_an_issue_or_pr_a_busy_slot_holds_is_not_given_out_again() -> None:
    cfg = config()
    slots = free(cfg)
    slots[0] = Slot("agent01", "author", "busy", Assignment("issue", 12))
    slots[3] = Slot("review01", "review", "failed", Assignment("review", 40))
    result = plan(cfg, slots, [issue(12)], [pull(40, 13)], State(), random.Random(1))
    assert result.assignments == []
    assert any("review01" in n and "failed" in n for n in result.notes)


def test_lanes_are_drawn_by_weight() -> None:
    cfg = config(slots=[f"agent{i:02d}" for i in range(1, 13)], lanes={"feature": 3, "tooling": 1})
    issues = [issue(i, "feature") for i in range(1, 41)] + [
        issue(i, "tooling") for i in range(41, 81)
    ]
    counts = {"feature": 0, "tooling": 0}
    for seed in range(200):
        for _, a in plan(cfg, free(cfg), issues, [], State(), random.Random(seed)).assignments:
            counts["feature" if a.number <= 40 else "tooling"] += 1
    share = counts["feature"] / (counts["feature"] + counts["tooling"])
    assert 0.70 < share < 0.80


def test_an_empty_lane_gives_its_turn_to_the_others() -> None:
    cfg = config(lanes={"feature": 100, "tooling": 1})
    result = plan(
        cfg, free(cfg), [issue(i, "tooling") for i in range(1, 4)], [], State(), random.Random(1)
    )
    assert len(result.assignments) == 3


# --- shares -------------------------------------------------------------------------------------


def test_two_thirds_of_author_slots_are_kept_for_fix_rounds() -> None:
    cfg = config()  # 3 author slots: 2 for work in flight, 1 for new issues
    pulls = [pull(40 + i, 10 + i, ci="fail") for i in range(5)]
    issues = [issue(i) for i in range(1, 6)]
    kinds = sorted(
        a.kind
        for _, a in plan(cfg, free(cfg), issues, pulls, State(), random.Random(1)).assignments
    )
    assert kinds == ["fix", "fix", "issue"]


def test_busy_slots_count_against_their_share() -> None:
    cfg = config()
    slots = free(cfg)
    slots[0] = Slot("agent01", "author", "busy", Assignment("fix", 30))
    slots[1] = Slot("agent02", "author", "busy", Assignment("fix", 31))
    pulls = [pull(40, 12, ci="fail")]
    result = plan(cfg, slots, [issue(1)], pulls, State(), random.Random(1))
    assert assigned(result) == {"agent03": "issue 1"}


def test_an_unused_share_spills_to_the_other_kind() -> None:
    cfg = config()
    only_fixes = plan(
        cfg,
        free(cfg),
        [],
        [pull(40 + i, 10 + i, ci="fail") for i in range(5)],
        State(),
        random.Random(1),
    )
    assert sorted(a.kind for _, a in only_fixes.assignments) == ["fix", "fix", "fix"]
    only_new = plan(cfg, free(cfg), [issue(i) for i in range(1, 6)], [], State(), random.Random(1))
    assert sorted(a.kind for _, a in only_new.assignments) == ["issue", "issue", "issue"]


def test_the_state_records_runs_and_reviewed_heads() -> None:
    cfg = config()
    result = plan(
        cfg,
        free(cfg),
        [issue(1)],
        [pull(40, 12), pull(41, 13, ci="fail")],
        State(),
        random.Random(1),
    )
    assert result.state.runs == {1: 1, 13: 1}
    assert result.state.reviewed == {40: ["a" * 40]}


def test_the_plan_does_not_change_the_state_it_was_given() -> None:
    cfg = config()
    state = State()
    plan(cfg, free(cfg), [issue(1)], [pull(40, 12)], state, random.Random(1))
    assert state.runs == {} and state.reviewed == {}
