"""Evasion kit for the verdict gate: every way to fake, replay or hide a review verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gates import verdict
from gates.verdict import Comment

HEAD = "a" * 40
OLD = "b" * 40
T1 = "2026-11-13T10:00:00Z"
T2 = "2026-11-13T11:00:00Z"
T3 = "2026-11-13T12:00:00Z"


def marker(sha: str = HEAD, v: str = "approve", **extra: object) -> str:
    return f"<!-- fleet-verdict v1 {json.dumps({'sha': sha, 'verdict': v, **extra})} -->"


def comment(
    body: str,
    *,
    id: int = 1,
    login: str = "glennmcd",
    created: str = T1,
    updated: str | None = None,
) -> Comment:
    return Comment(id=id, login=login, body=body, created_at=created, updated_at=updated or created)


def decide(*comments: Comment, **kw: Any) -> verdict.Decision:
    kw.setdefault("reviewers", ["glennmcd"])
    return verdict.decide(HEAD, list(comments), **kw)


def passes(*comments: Comment, **kw: Any) -> bool:
    return decide(*comments, **kw).conclusion == "success"


# --- the happy path and the plain failures


def test_an_approval_at_head_passes() -> None:
    assert passes(comment(marker() + "\n\nLooks right. No findings."))


def test_no_comments_fails_and_says_why() -> None:
    d = decide()
    assert d.conclusion == "failure" and "no verdict" in d.summary.lower()


def test_request_changes_at_head_fails() -> None:
    d = decide(comment(marker(v="request_changes")))
    assert d.conclusion == "failure" and "request" in d.summary.lower()


def test_a_push_after_approval_resets_it() -> None:
    d = verdict.decide(HEAD, [comment(marker(sha=OLD))], reviewers=["glennmcd"])
    assert d.conclusion == "failure" and OLD[:7] in d.summary


# --- order of verdicts


def test_the_newest_verdict_wins_not_the_first_in_the_list() -> None:
    late_approve = comment(marker(), id=2, created=T2)
    early_changes = comment(marker(v="request_changes"), id=1, created=T1)
    assert passes(late_approve, early_changes)
    assert passes(early_changes, late_approve)


def test_an_approval_followed_by_request_changes_fails() -> None:
    assert not passes(
        comment(marker(), id=1, created=T1), comment(marker(v="request_changes"), id=2, created=T2)
    )


def test_a_tie_in_time_goes_to_the_higher_comment_id() -> None:
    assert not passes(comment(marker(), id=1), comment(marker(v="request_changes"), id=2))


def test_a_request_changes_for_an_old_head_does_not_cancel_the_approval() -> None:
    assert passes(
        comment(marker(), id=1, created=T1),
        comment(marker(OLD, "request_changes"), id=2, created=T2),
    )


# --- who may speak


@pytest.mark.parametrize(
    "login", ["someone-else", "glennmcd2", "glennmcd-bot", "xglennmcd", "", "ghost"]
)
def test_other_authors_are_ignored(login: str) -> None:
    assert not passes(comment(marker(), login=login))


def test_login_case_does_not_matter() -> None:
    assert passes(comment(marker(), login="GlennMcD"))


def test_an_unlisted_author_cannot_override_the_reviewer() -> None:
    assert passes(
        comment(marker(), id=1, created=T1),
        comment(marker(v="request_changes"), id=2, created=T2, login="x"),
    )


def test_no_reviewers_configured_fails_closed() -> None:
    assert not passes(comment(marker()), reviewers=[])


def test_a_missing_config_file_is_an_error_not_an_empty_config(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not found"):
        verdict.main(["--repo", "o/r", "--pr", "1", "--root", str(tmp_path)])
    with pytest.raises(SystemExit, match="not found"):
        verdict.main(["--repo", "o/r", "--pr", "1", "--root", str(tmp_path), "--config", "x.json"])


def test_a_config_that_is_not_json_is_an_error(tmp_path: Path) -> None:
    (tmp_path / ".fleet-gates.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(SystemExit, match="not valid JSON"):
        verdict.main(["--repo", "o/r", "--pr", "1", "--root", str(tmp_path)])


# --- the SHA must be the full head SHA


@pytest.mark.parametrize(
    "sha",
    [HEAD[:7], HEAD[:12], HEAD[:39], HEAD + "a", HEAD.upper(), " " + HEAD, "", "HEAD", "main"],
)
def test_only_the_full_lowercase_head_sha_counts(sha: str) -> None:
    assert not passes(comment(marker(sha)))


def test_one_wrong_character_fails() -> None:
    assert not passes(comment(marker("a" * 39 + "b")))


# --- where the marker may sit


FENCED = "```\n" + marker() + "\n```"
TILDE = "~~~\n" + marker() + "\n~~~"
QUOTED = "> " + marker()
INDENTED = "    " + marker()
INLINE = "I approve " + marker()
TRAILING = marker() + " and more"
SPOOFED = {
    "fenced": FENCED,
    "tilde fence": TILDE,
    "blockquote": QUOTED,
    "nested quote": ">> " + marker(),
    "indented code": INDENTED,
    "tab indented": "\t" + marker(),
    "inline": INLINE,
    "trailing text": TRAILING,
    "inside a list": "- " + marker(),
}


@pytest.mark.parametrize("name", SPOOFED)
def test_a_marker_that_is_not_a_line_of_its_own_is_ignored(name: str) -> None:
    assert not passes(comment(SPOOFED[name])), name


def test_a_marker_after_a_closed_fence_counts() -> None:
    assert passes(comment("```\nexample\n```\n" + marker()))


def test_a_marker_in_the_middle_of_prose_lines_counts() -> None:
    assert passes(comment("Summary\n\n" + marker() + "\n\nDetails"))


def test_a_reviewer_quoting_an_old_approval_does_not_approve_the_new_head() -> None:
    reply = "Earlier I wrote:\n> " + marker() + "\nbut I found a bug."
    assert not passes(comment(reply))


def test_two_markers_in_one_comment_are_ambiguous_and_ignored() -> None:
    assert not passes(comment(marker() + "\n" + marker(v="request_changes")))
    assert not passes(comment(marker() + "\n" + marker()))


# --- the marker's own shape


@pytest.mark.parametrize(
    "body",
    [
        '<!-- fleet-verdict v2 {"sha": "%s", "verdict": "approve"} -->' % HEAD,
        '<!-- fleet-verdict {"sha": "%s", "verdict": "approve"} -->' % HEAD,
        '<!-- Fleet-Verdict v1 {"sha": "%s", "verdict": "approve"} -->' % HEAD,
        '<!-- fleet‑verdict v1 {"sha": "%s", "verdict": "approve"} -->' % HEAD,
        '<!-- fleet-verdict v1 {"sha": "%s", "verdict": "APPROVE"} -->' % HEAD,
        '<!-- fleet-verdict v1 {"sha": "%s", "verdict": "approved"} -->' % HEAD,
        '<!-- fleet-verdict v1 {"sha": "%s", "verdict": "approve "} -->' % HEAD,
        '<!-- fleet-verdict v1 {"sha": "%s", "verdict": "lgtm"} -->' % HEAD,
        '<!-- fleet-verdict v1 {"sha": "%s"} -->' % HEAD,
        '<!-- fleet-verdict v1 {"verdict": "approve"} -->',
        '<!-- fleet-verdict v1 {"sha": %d, "verdict": "approve"} -->' % 1,
        '<!-- fleet-verdict v1 {"sha": ["%s"], "verdict": "approve"} -->' % HEAD,
        "<!-- fleet-verdict v1 {not json} -->",
        "<!-- fleet-verdict v1 [] -->",
        '<!-- fleet-verdict v1 {"sha": "%s", "verdict": "approve"}' % HEAD,
        "",
    ],
)
def test_a_malformed_marker_never_approves(body: str) -> None:
    assert not passes(comment(body))


@pytest.mark.parametrize(
    "body",
    [
        marker(),
        marker().replace("<!-- ", "<!--").replace(" -->", "-->"),
        marker().replace("<!-- ", "<!--   ").replace(" -->", "   -->"),
        marker().replace(" v1 ", "\t v1  "),
        marker(reviewer="code-reviewer", findings=0),
        marker().replace("\n", "\r\n") + "\r\n\r\nBody\r\n",
    ],
)
def test_harmless_spelling_differences_still_count(body: str) -> None:
    assert passes(comment(body))


def test_the_deleted_user_or_empty_body_does_not_crash() -> None:
    assert not passes(Comment(id=1, login="", body="", created_at=T1, updated_at=T1))
    assert not passes(Comment(id=1, login="glennmcd", body=None, created_at=T1, updated_at=T1))  # type: ignore[arg-type]


# --- edits


def test_an_edited_comment_is_ignored() -> None:
    assert not passes(comment(marker(), created=T1, updated=T2))


def test_an_untouched_comment_counts() -> None:
    assert passes(comment(marker(), created=T1, updated=T1))


# --- draft, exempt branches


def test_a_draft_pull_request_fails_even_with_an_approval() -> None:
    d = decide(comment(marker()), draft=True)
    assert d.conclusion == "failure" and "draft" in d.summary.lower()


@pytest.mark.parametrize(
    "ref", ["release-please--branches--main", "release-please--branches--main--components--x"]
)
def test_an_exempt_branch_passes_without_a_verdict(ref: str) -> None:
    assert passes(head_ref=ref, exempt_prefixes=["release-please--"])


@pytest.mark.parametrize(
    "ref",
    [
        "feature/release-please--x",
        "release-please-x",
        "xrelease-please--x",
        "RELEASE-PLEASE--x",
        "",
        "main",
    ],
)
def test_only_a_real_prefix_exempts(ref: str) -> None:
    assert not passes(head_ref=ref, exempt_prefixes=["release-please--"])


def test_an_empty_prefix_is_not_a_wildcard() -> None:
    assert not passes(head_ref="anything", exempt_prefixes=[""])


def test_a_draft_exempt_branch_still_fails() -> None:
    assert not passes(
        head_ref="release-please--x", exempt_prefixes=["release-please--"], draft=True
    )


# --- publishing the check run


class FakeGh:
    """Stands in for the GitHub API: records every call and returns canned replies."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, path: str, fields: dict[str, str] | None = None) -> Any:
        self.calls.append((method, path, fields or {}))
        for key, reply in self.replies.items():
            if f"{method} {path}".startswith(key):
                return reply
        raise AssertionError(f"unexpected call {method} {path}")


def api(runs: list[dict[str, Any]], comments: list[dict[str, Any]], **pr: Any) -> FakeGh:
    pull = {"head": {"sha": HEAD, "ref": "feat/x"}, "draft": False, **pr}
    return FakeGh(
        {
            "GET repos/o/r/pulls/7": pull,
            "GET repos/o/r/issues/7/comments": comments,
            f"GET repos/o/r/commits/{HEAD}/check-runs": {"check_runs": runs},
            "POST repos/o/r/check-runs": {"id": 99},
            "PATCH repos/o/r/check-runs/55": {"id": 55},
        }
    )


RAW_APPROVAL = [
    {"id": 1, "user": {"login": "glennmcd"}, "body": marker(), "created_at": T1, "updated_at": T1}
]
CFG = {"verdict": {"reviewers": ["glennmcd"], "exempt_branches": ["release-please--"]}}


def test_publish_creates_a_check_run_on_the_live_head_sha() -> None:
    gh = api([], RAW_APPROVAL)
    verdict.run("o/r", 7, CFG, gh)
    method, path, fields = gh.calls[-1]
    assert (method, path) == ("POST", "repos/o/r/check-runs")
    assert fields["name"] == "reviewer-verdict" and fields["head_sha"] == HEAD
    assert fields["conclusion"] == "success" and fields["status"] == "completed"


def test_publish_updates_the_existing_run_instead_of_adding_a_second() -> None:
    gh = api([{"id": 55, "name": "reviewer-verdict"}], RAW_APPROVAL)
    verdict.run("o/r", 7, CFG, gh)
    assert gh.calls[-1][:2] == ("PATCH", "repos/o/r/check-runs/55")


def test_publish_reports_failure_when_the_head_has_no_verdict() -> None:
    gh = api([], [])
    verdict.run("o/r", 7, CFG, gh)
    assert gh.calls[-1][2]["conclusion"] == "failure"


def test_publish_ignores_other_checks_named_differently() -> None:
    gh = api([{"id": 55, "name": "backend"}], RAW_APPROVAL)
    verdict.run("o/r", 7, CFG, gh)
    assert gh.calls[-1][:2] == ("POST", "repos/o/r/check-runs")


def test_publish_honours_the_exempt_branch_from_the_live_pull_request() -> None:
    gh = FakeGh(
        {
            "GET repos/o/r/pulls/7": {
                "head": {"sha": HEAD, "ref": "release-please--branches--main"},
                "draft": False,
            },
            "GET repos/o/r/issues/7/comments": [],
            f"GET repos/o/r/commits/{HEAD}/check-runs": {"check_runs": []},
            "POST repos/o/r/check-runs": {"id": 1},
        }
    )
    verdict.run("o/r", 7, CFG, gh)
    assert gh.calls[-1][2]["conclusion"] == "success"
