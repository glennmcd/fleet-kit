"""Tests for scripts/fleetwright_report.py, on fixed transcripts written to a temp folder."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fleetwright_report as fr  # noqa: E402


def record(
    rid: str,
    stamp: str,
    *,
    branch: str = "feat/a",
    inp: int = 0,
    read: int = 0,
    write: int = 0,
    write_1h: int = 0,
    out: int = 0,
    model: str | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
        "output_tokens": out,
    }
    if write_1h:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": write - write_1h,
            "ephemeral_1h_input_tokens": write_1h,
        }
    return {
        "type": "assistant",
        "requestId": rid,
        "timestamp": stamp,
        "gitBranch": branch,
        "message": {"usage": usage, **({"model": model} if model else {})},
    }


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, records: list[dict[str, Any]], junk: bool = False) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records]
        if junk:
            lines.insert(1, "not json")
            lines.append(json.dumps({"type": "user", "message": {"content": "hi"}}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)


class ReadRequests(Fixture):
    def test_each_request_counted_once(self) -> None:
        a = record("r1", "2026-11-10T10:00:00Z", inp=10, read=100, write=50, out=5)
        path = self.write("proj/s1.jsonl", [a, a, a], junk=True)
        requests = fr.read_requests([path])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].context, 160)
        self.assertEqual(requests[0].raw, 165)

    def test_one_hour_writes_are_split_out(self) -> None:
        r = record("r1", "2026-11-10T10:00:00Z", write=300, write_1h=200)
        req = fr.read_requests([self.write("p/s.jsonl", [r])])[0]
        self.assertEqual((req.cache_write_5m, req.cache_write_1h), (100, 200))

    def test_subagent_transcripts_are_marked(self) -> None:
        main = self.write("p/s.jsonl", [record("m", "2026-11-10T10:00:00Z")])
        sub = self.write("p/s/subagents/agent-x.jsonl", [record("s", "2026-11-10T10:01:00Z")])
        flags = {r.request_id: r.is_subagent for r in fr.read_requests([main, sub])}
        self.assertEqual(flags, {"m": False, "s": True})

    def test_synthetic_records_are_not_calls(self) -> None:
        # Claude Code writes `<synthetic>` assistant records (zero usage) for replies no model made.
        # Today they carry no requestId; skip them by model too, so they never count as a call or a
        # zero-write first turn.
        fake = record("syn", "2026-11-10T09:59:00Z")
        fake["message"]["model"] = "<synthetic>"
        real = record("r1", "2026-11-10T10:00:00Z", read=100, write=50)
        requests = fr.read_requests([self.write("p/s.jsonl", [fake, real])])
        self.assertEqual([r.request_id for r in requests], ["r1"])


def prompt(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


REVIEW_57 = (
    "Run the cc-practices review-pr skill on pull request #57 of this repository. The clone is "
    "checked out at the pull request's head commit."
)


class ReviewRuns(Fixture):
    def test_the_agent_review_prompt_names_its_pr(self) -> None:
        # The reviewer link depends on fleetwright_agent's fixed prompt; if its wording changes,
        # this fails instead of the report silently dropping every review.
        import fleetwright_agent

        self.assertEqual(fr.review_pr(fleetwright_agent.review_prompt(57)), 57)

    def test_a_review_transcript_and_its_subagents_carry_the_pr(self) -> None:
        when = "2026-11-10T10:00:00Z"
        main = self.write(
            "p/rev.jsonl",
            [prompt(REVIEW_57), record("r1", when, branch="HEAD")],
        )
        sub = self.write("p/rev/subagents/agent-x.jsonl", [record("r2", when, branch="HEAD")])
        author = self.write("p/auth.jsonl", [prompt("Work on issue 52"), record("a1", when)])
        found = {r.request_id: r.review_pr for r in fr.read_requests([main, sub, author])}
        self.assertEqual(found, {"r1": 57, "r2": 57, "a1": None})

    def test_list_content_and_meta_records_are_handled(self) -> None:
        when = "2026-11-10T10:00:00Z"
        path = self.write(
            "p/rev.jsonl",
            [
                prompt("<system-reminder>hook output</system-reminder>", isMeta=True),
                {"type": "user", "message": {"content": [{"type": "text", "text": REVIEW_57}]}},
                record("r1", when, branch="HEAD"),
            ],
        )
        self.assertEqual(fr.read_requests([path])[0].review_pr, 57)

    def test_lookalike_prompts_are_not_reviews(self) -> None:
        when = "2026-11-10T10:00:00Z"
        quoted = self.write(
            "p/a.jsonl", [prompt(f"Please do this: {REVIEW_57}"), record("a", when)]
        )
        later = self.write("p/b.jsonl", [prompt("hello"), prompt(REVIEW_57), record("b", when)])
        found = {r.request_id: r.review_pr for r in fr.read_requests([quoted, later])}
        self.assertEqual(found, {"a": None, "b": None})


class Weighting(unittest.TestCase):
    def test_prices_apply_per_kind(self) -> None:
        when = fr.parse_time("2026-11-10T10:00:00Z")
        req = fr.Request(
            "r",
            when,
            "t",
            "b",
            False,
            input=10,
            cache_read=1000,
            cache_write_5m=100,
            cache_write_1h=50,
            output=20,
        )
        # 10*1 + 1000*0.1 + 100*1.25 + 50*2 + 20*5
        self.assertAlmostEqual(req.weighted(fr.PRICES), 10 + 100 + 125 + 100 + 100)


class Expiry(Fixture):
    def test_long_gap_with_big_rewrite_is_expiry(self) -> None:
        path = self.write(
            "p/s/subagents/agent-a.jsonl",
            [
                record("a1", "2026-11-10T10:00:00Z", read=90_000, write=1_000),
                record("a2", "2026-11-10T10:06:00Z", read=2_000, write=90_000),
            ],
        )
        self.assertEqual(fr.expiry_ids(fr.read_requests([path])), {"a2"})

    def test_long_gap_that_still_hits_the_cache_is_not(self) -> None:
        path = self.write(
            "p/s.jsonl",
            [
                record("a1", "2026-11-10T10:00:00Z", read=90_000, write=1_000),
                record("a2", "2026-11-10T10:20:00Z", read=90_000, write=1_000),
            ],
        )
        self.assertEqual(fr.expiry_ids(fr.read_requests([path])), set())

    def test_short_gap_is_not(self) -> None:
        path = self.write(
            "p/s.jsonl",
            [
                record("a1", "2026-11-10T10:00:00Z", write=50_000),
                record("a2", "2026-11-10T10:04:00Z", write=60_000),
            ],
        )
        self.assertEqual(fr.expiry_ids(fr.read_requests([path])), set())

    def test_one_hour_write_is_judged_against_an_hour(self) -> None:
        path = self.write(
            "p/s.jsonl",
            [
                record("a1", "2026-11-10T10:00:00Z", write=50_000),
                record("a2", "2026-11-10T10:30:00Z", write=60_000, write_1h=60_000),
            ],
        )
        self.assertEqual(fr.expiry_ids(fr.read_requests([path])), set())

    def test_gaps_are_measured_within_one_transcript(self) -> None:
        a = self.write("p/a.jsonl", [record("a1", "2026-11-10T10:00:00Z", write=10)])
        b = self.write("p/b.jsonl", [record("b1", "2026-11-10T11:00:00Z", write=10)])
        self.assertEqual(fr.expiry_ids(fr.read_requests([a, b])), set())


class PullRequests(Fixture):
    def test_lines_are_capped_and_unmerged_skipped(self) -> None:
        path = self.root / "prs.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "number": 1,
                        "headRefName": "feat/a",
                        "mergedAt": "2026-11-11T09:00:00Z",
                        "additions": 9_000,
                        "deletions": 1_000,
                    },
                    {
                        "number": 2,
                        "headRefName": "feat/b",
                        "mergedAt": None,
                        "additions": 5,
                        "deletions": 5,
                    },
                ]
            ),
            encoding="utf-8",
        )
        prs = fr.read_prs(str(path))
        self.assertEqual([(p.number, p.lines, p.week) for p in prs], [(1, 5_000, "2026-W46")])
        # The branch of every PR, merged or not, so an open PR's work can be selected too.
        self.assertEqual(fr.read_pr_branches(str(path)), {1: "feat/a", 2: "feat/b"})


class SelectPrs(Fixture):
    def build(self) -> list[fr.Request]:
        when = "2026-11-10T10:00:00Z"
        paths = [
            self.write("p/a52.jsonl", [record("a52", when, branch="issue/52")]),
            self.write("p/a53.jsonl", [record("a53", when, branch="issue/53")]),
            self.write("p/a54.jsonl", [record("a54", when, branch="issue/54")]),
            self.write("p/r57.jsonl", [prompt(REVIEW_57), record("r57", when, branch="HEAD")]),
            self.write(
                "p/r58.jsonl",
                [prompt(REVIEW_57.replace("#57", "#58")), record("r58", when, branch="HEAD")],
            ),
        ]
        return fr.read_requests(paths)

    def test_author_and_reviewer_calls_of_each_pr_are_kept(self) -> None:
        requests = self.build()
        prs = [
            fr.PullRequest(57, "issue/52", "2026-W46", 10),
            fr.PullRequest(58, "issue/53", "2026-W46", 20),
            fr.PullRequest(59, "issue/54", "2026-W46", 30),
        ]
        branches = {57: "issue/52", 58: "issue/53", 59: "issue/54"}
        kept, kept_prs, warnings = fr.select_prs(requests, prs, branches, {57, 58})
        self.assertEqual(sorted(r.request_id for r in kept), ["a52", "a53", "r57", "r58"])
        self.assertEqual([p.number for p in kept_prs], [57, 58])
        self.assertEqual(warnings, [])

    def test_unknown_pr_and_shared_branch_are_warned(self) -> None:
        requests = self.build()
        branches = {57: "issue/52", 60: "issue/52"}
        kept, _, warnings = fr.select_prs(requests, [], branches, {57, 58})
        self.assertEqual(sorted(r.request_id for r in kept), ["a52", "r57", "r58"])
        self.assertEqual(len(warnings), 2)
        self.assertIn("#58", warnings[0])
        self.assertIn("#60", warnings[1])


class TwoWeeks(Fixture):
    """Two weeks of calls on two branches, and three merged PRs."""

    def json_report(self, *args: str) -> dict[str, Any]:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(fr.main([*args, "--json"]), 0)
        report: dict[str, Any] = json.loads(out.getvalue())
        return report

    def build(self) -> tuple[list[fr.Request], list[fr.PullRequest]]:
        week45 = self.write(
            "p/w45.jsonl",
            [
                record(
                    "x1", "2026-11-03T10:00:00Z", branch="feat/old", inp=100, write=10_000, out=100
                ),
                record("x2", "2026-11-03T10:01:00Z", branch="feat/old", read=200_000, out=100),
            ],
        )
        week46 = self.write(
            "p/w46.jsonl",
            [
                record("y1", "2026-11-10T10:00:00Z", branch="feat/a", inp=100, write=5_000, out=50),
                record("y2", "2026-11-10T10:01:00Z", branch="feat/a", read=50_000, out=50),
            ],
        )
        prs = [
            fr.PullRequest(1, "feat/old", "2026-W45", 100),
            fr.PullRequest(2, "feat/a", "2026-W46", 100),
            fr.PullRequest(3, "feat/b", "2026-W46", 100),
        ]
        return fr.read_requests([week45, week46]), prs


class Report(TwoWeeks):
    def test_weeks_index_and_distribution(self) -> None:
        requests, prs = self.build()
        rows = fr.week_rows(requests, prs, fr.PRICES)
        fr.add_index(rows, ["2026-W45"])
        w45, w46 = rows
        self.assertEqual((w45["week"], w46["week"]), ("2026-W45", "2026-W46"))
        self.assertEqual(w45["weighted_index"], 100)
        self.assertEqual(w46["merged_prs"], 2)
        # W45: 100 + 12_500 + 20_000 + 1_000 = 33_600 per PR; W46: (100 + 6_250 + 5_000 + 500) / 2
        self.assertEqual(w45["weighted"], 33_600)
        self.assertEqual(w46["weighted_per_pr_index"], round(100 * 5_925 / 33_600))
        self.assertEqual(w45["over_ceiling_share"], 0.5)
        self.assertEqual(w45["first_turn_write"], 10_000)

    def test_branches_join_to_their_pr(self) -> None:
        requests, prs = self.build()
        rows = {r["branch"]: r for r in fr.branch_rows(requests, prs, fr.PRICES)}
        self.assertEqual(rows["feat/a"]["pr"], 2)
        self.assertAlmostEqual(rows["feat/a"]["weighted_per_line"], 11_850 / 100)

    def test_cli_prints_tables_and_json(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            code = fr.main(["--root", str(self.root), "--baseline", "2026-W45"])
        self.assertEqual(code, 0)
        self.assertIn("| 2026-W46 |", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(["--root", str(self.root), "--json", "--price", "output=4"])
        self.assertEqual(json.loads(out.getvalue())["weeks"][0]["weighted"], 33_600 - 200)

    def test_cli_names_the_context_threshold(self) -> None:
        # The share column counts calls at or over --ceiling (default 150k, the hook's advisory
        # level), not the 200k hard ceiling; its header says which.
        self.build()
        for args, header in (([], "Ctx >=150k"), (["--ceiling", "200000"], "Ctx >=200k")):
            out = io.StringIO()
            with redirect_stdout(out):
                fr.main(["--root", str(self.root), *args])
            self.assertIn(f"| {header} |", out.getvalue())
            self.assertNotIn("Over ceiling", out.getvalue())

    def test_cli_reads_every_root_and_each_may_be_a_pattern(self) -> None:
        # The report unit reads every slot's transcripts (agentNN, reviewNN) and no one else's.
        when = "2026-11-10T10:00:00Z"
        self.write("home/agent01/.claude/projects/p/a.jsonl", [record("a", when)])
        self.write("home/agent02/.claude/projects/p/b.jsonl", [record("b", when)])
        self.write("home/review01/.claude/projects/p/c.jsonl", [record("c", when)])
        self.write("home/bob/.claude/projects/p/d.jsonl", [record("d", when)])
        home = self.root / "home"
        report = self.json_report(
            "--root",
            f"{home}/agent[0-9][0-9]/.claude/projects",
            "--root",
            f"{home}/review[0-9][0-9]/.claude/projects",
        )
        self.assertEqual(report["weeks"][0]["requests"], 3)

    def test_project_matches_below_each_root_only(self) -> None:
        when = "2026-11-10T10:00:00Z"
        self.write("slotx/projects/-home-agent01-repo/a.jsonl", [record("a", when)])
        self.write("slotx/projects/-tmp-other/b.jsonl", [record("b", when)])
        root = str(self.root / "slotx" / "projects")
        report = self.json_report("--root", root, "--project", "repo")
        self.assertEqual(report["weeks"][0]["requests"], 1)
        # The root's own path is not searched: "slotx" is above it.
        with redirect_stderr(io.StringIO()):
            self.assertEqual(fr.main(["--root", root, "--project", "slotx"]), 1)

    def test_a_missing_prs_file_says_how_to_make_it(self) -> None:
        self.build()
        err = io.StringIO()
        with redirect_stderr(err):
            code = fr.main(["--root", str(self.root), "--prs", str(self.root / "none.json")])
        self.assertEqual(code, 2)
        self.assertIn("gh pr list --state merged", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_cli_pr_filter_counts_author_and_reviewer(self) -> None:
        self.build()
        self.write(
            "p/r2.jsonl",
            [
                prompt(REVIEW_57.replace("#57", "#2")),
                record("rv", "2026-11-11T10:00:00Z", branch="HEAD", read=1_000),
            ],
        )
        prs = self.root / "prs.json"
        prs.write_text(
            json.dumps(
                [
                    {
                        "number": 1,
                        "headRefName": "feat/old",
                        "mergedAt": "2026-11-04T09:00:00Z",
                        "additions": 50,
                        "deletions": 50,
                    },
                    {
                        "number": 2,
                        "headRefName": "feat/a",
                        "mergedAt": "2026-11-11T09:00:00Z",
                        "additions": 60,
                        "deletions": 40,
                    },
                ]
            ),
            encoding="utf-8",
        )
        report = self.json_report("--root", str(self.root), "--prs", str(prs), "--pr", "2")
        self.assertEqual(report["prs"], [2])
        self.assertEqual([w["week"] for w in report["weeks"]], ["2026-W46"])
        week = report["weeks"][0]
        # y1, y2 on feat/a, plus the reviewer's call on the detached head.
        self.assertEqual((week["requests"], week["merged_prs"], week["changed_lines"]), (3, 1, 100))
        self.assertEqual([b["branch"] for b in report["branches"]], ["feat/a", "HEAD"])

    def test_cli_pr_filter_without_pr_data_warns(self) -> None:
        self.write(
            "p/r2.jsonl",
            [prompt(REVIEW_57.replace("#57", "#2")), record("rv", "2026-11-11T10:00:00Z")],
        )
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = fr.main(["--root", str(self.root), "--pr", "2", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("reviewer calls only", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["weeks"][0]["requests"], 1)

    def test_cli_pr_filter_with_no_matching_calls_fails_cleanly(self) -> None:
        self.build()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(fr.main(["--root", str(self.root), "--pr", "99"]), 1)
        self.assertIn("#99", err.getvalue())

    def test_cli_with_no_transcripts_fails_cleanly(self) -> None:
        empty = os.path.join(self._tmp.name, "empty")
        os.mkdir(empty)
        self.assertEqual(fr.main(["--root", empty]), 1)


class Totals(TwoWeeks):
    def test_total_row_covers_every_week_at_once(self) -> None:
        requests, prs = self.build()
        total = fr.total_row(requests, prs, fr.PRICES)
        self.assertEqual(total["week"], "Total")
        self.assertEqual((total["requests"], total["merged_prs"]), (4, 3))
        # x1 10_200 + x2 200_100 + y1 5_150 + y2 50_050
        self.assertEqual(total["raw_tokens"], 265_500)
        self.assertEqual(total["raw_per_line"], 265_500 / 300)
        self.assertEqual(total["weighted"], 33_600 + 11_850)
        # Contexts 10_100, 200_000, 5_100, 50_000: a median over all of them, not of weekly medians.
        self.assertEqual(total["median_context"], 30_050)

    def test_cache_read_share_of_cost_beside_share_of_tokens(self) -> None:
        requests, prs = self.build()
        total = fr.total_row(requests, prs, fr.PRICES)
        # Reads 250_000 tokens at 0.1 = 25_000 of 45_450 weighted.
        self.assertAlmostEqual(total["cache_read_spend_share"], 25_000 / 45_450)
        self.assertAlmostEqual(total["cache_read_share"], 250_000 / 265_500)

    def test_author_first_turn_leaves_out_reviewers_and_subagents(self) -> None:
        when = "2026-11-10T10:00:00Z"
        author = self.write("p/a.jsonl", [record("a1", when, read=20_000, write=3_000)])
        sub = self.write("p/a/subagents/agent-x.jsonl", [record("s1", when, write=1_000)])
        review = self.write(
            "p/r.jsonl", [prompt(REVIEW_57), record("r1", when, branch="HEAD", write=90_000)]
        )
        requests = fr.read_requests([author, sub, review])
        total = fr.total_row(requests, [], fr.PRICES)
        self.assertEqual(total["author_first_context"], 23_000)
        week = fr.week_rows(requests, [], fr.PRICES)[0]
        self.assertEqual(week["author_first_context"], 23_000)

    def test_cli_shows_the_total_row_and_new_columns(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(["--root", str(self.root), "--baseline", "2026-W45"])
        text = out.getvalue()
        self.assertIn("| Total |", text)
        for header in ("Raw tokens", "Raw/line", "Reads % of cost", "Author 1st ctx"):
            self.assertIn(header, text)
        report = self.json_report("--root", str(self.root), "--baseline", "2026-W45")
        self.assertEqual(report["total"]["raw_tokens"], 265_500)
        # A total of many weeks against one baseline week's average does not compare.
        self.assertIsNone(report["total"]["weighted_index"])

    def test_total_is_indexed_per_pr_against_the_baseline(self) -> None:
        requests, prs = self.build()
        total = fr.total_row(requests, prs, fr.PRICES)
        fr.add_index([*fr.week_rows(requests, prs, fr.PRICES), total], ["2026-W45"])
        self.assertEqual(total["weighted_per_pr_index"], round(100 * (45_450 / 3) / 33_600))


def price_table(*items: str) -> fr.PriceTable:
    return fr.price_table([fr.parse_model_price(i) for i in items], fr.PRICES)


def opus_rates(item: str) -> fr.Rates:
    found = fr.model_rates("claude-opus-5-5", price_table(item))
    assert found is not None
    return found


class Models(Fixture):
    HAIKU, OPUS = "claude-haiku-5-5", "claude-opus-5-5"

    def build(self) -> list[fr.Request]:
        when = "2026-11-10T10:00:00Z"
        author = self.write(
            "p/a.jsonl",
            [
                record("a1", when, branch="issue/52", inp=1_000, out=200, model=self.HAIKU),
                record("a2", when, branch="issue/52", read=10_000, model=self.HAIKU),
            ],
        )
        review = self.write(
            "p/r.jsonl",
            [prompt(REVIEW_57), record("r1", when, branch="HEAD", inp=2_000, model=self.OPUS)],
        )
        return fr.read_requests([author, review])

    def test_each_call_keeps_its_model(self) -> None:
        models = {r.request_id: r.model for r in self.build()}
        self.assertEqual(models, {"a1": self.HAIKU, "a2": self.HAIKU, "r1": self.OPUS})
        bare = fr.read_requests([self.write("p/x.jsonl", [record("x", "2026-11-10T10:00:00Z")])])
        self.assertEqual(bare[0].model, "")

    def test_model_rates_prefer_an_exact_name_then_the_longest_part(self) -> None:
        rates = price_table("haiku=1", "claude-haiku-5-5=0.8", "opus=5")

        def input_rate(model: str) -> float | None:
            found = fr.model_rates(model, rates)
            return found.input if found else None

        self.assertEqual(input_rate(self.HAIKU), 0.8)
        self.assertEqual(input_rate("claude-haiku-4-5"), 1.0)
        self.assertEqual(input_rate(self.OPUS), 5.0)
        self.assertIsNone(input_rate("claude-sonnet-5-5"))

    def test_one_value_prices_the_other_kinds_by_the_weights(self) -> None:
        rates = opus_rates("opus=4")
        self.assertEqual(
            (rates.input, rates.cache_read, rates.cache_write_5m, rates.cache_write_1h),
            (4.0, 0.4, 5.0, 8.0),
        )
        self.assertEqual(rates.output, 20.0)

    def test_four_values_price_each_kind(self) -> None:
        rates = opus_rates("opus=4:0.2:5:20")
        self.assertEqual(
            (rates.input, rates.cache_read, rates.cache_write_5m, rates.output),
            (4.0, 0.2, 5.0, 20.0),
        )
        # The one-hour write is not given: 2x input, as the weights have it (1.6x the 5-minute).
        self.assertAlmostEqual(rates.cache_write_1h, 8.0)
        when = fr.parse_time("2026-11-10T10:00:00Z")
        call = fr.Request("r", when, "t", "b", False, 1_000, 10_000, 2_000, 0, 100, model=self.OPUS)
        cost = fr.call_cost(call, fr.PRICES, price_table("opus=4:0.2:5:20"))
        # 1_000 x 4 + 10_000 x 0.2 + 2_000 x 5 + 100 x 20 = 18_000 per million.
        self.assertAlmostEqual(cost or 0.0, 0.018)

    def test_malformed_prices_are_refused(self) -> None:
        import argparse

        bad = (
            "opus",
            "=4",
            "opus=x",
            "opus=4:0.2",
            "opus=4:0.2:5:20:9",
            "opus=-1",
            "haiku>=1",
            "haiku>x=1",
            "haiku>0=1",
            ">100000=1",
        )
        for text in bad:
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                fr.parse_model_price(text)

    def test_read_share_of_cost_is_in_dollars_when_priced(self) -> None:
        table = price_table("opus=4:0.2:5:20", "haiku=0.1:0.01:0.125:0.5")
        total = fr.total_row(self.build(), [], fr.PRICES, model_prices=table)
        # a1 Haiku 1_000 x 0.1 + 200 x 0.5 = 200; a2 Haiku 10_000 reads x 0.01 = 100;
        # r1 Opus 2_000 x 4 = 8_000 (all per million). Reads: 100 of 8_300.
        self.assertAlmostEqual(total["cost_usd"], 0.0083)
        self.assertAlmostEqual(total["cache_read_spend_share"], 100 / 8_300)
        # Unpriced, it stays a share of weighted tokens: 1_000 of 5_000.
        unpriced = fr.total_row(self.build(), [], fr.PRICES)
        self.assertAlmostEqual(unpriced["cache_read_spend_share"], 1_000 / 5_000)

    def test_a_size_tier_prices_prompts_over_its_threshold(self) -> None:
        self.assertEqual(
            fr.parse_model_price("haiku>100000=0.5:0.05:0.625:2.5"),
            ("haiku", 100_000, (0.5, 0.05, 0.625, 2.5)),
        )
        table = price_table("haiku=0.1:0.01:0.125:0.5", "haiku>100000=0.5:0.05:0.625:2.5")
        when = fr.parse_time("2026-11-10T10:00:00Z")

        def cost(read: int) -> float | None:
            # A prompt of `read` cached tokens plus 1_000 fresh input.
            call = fr.Request("r", when, "t", "b", False, 1_000, read, 0, 0, 0, model=self.HAIKU)
            return fr.call_cost(call, fr.PRICES, table)

        # 100_000 tokens is at the threshold, not over it: the base rates.
        self.assertAlmostEqual(cost(99_000) or 0.0, (1_000 * 0.1 + 99_000 * 0.01) / 1e6)
        self.assertAlmostEqual(cost(99_001) or 0.0, (1_000 * 0.5 + 99_001 * 0.05) / 1e6)

    def test_a_tier_alone_leaves_smaller_prompts_unpriced(self) -> None:
        self.build()
        err = io.StringIO()
        with redirect_stderr(err):
            code = fr.main(
                [
                    "--root",
                    str(self.root),
                    "--model-price",
                    "haiku>100000=1",
                    "--model-price",
                    "opus=5",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn(self.HAIKU, err.getvalue())

    def test_json_echoes_tiers_as_written(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(
                [
                    "--root",
                    str(self.root),
                    "--json",
                    "--model-price",
                    "haiku=0.1:0.01:0.125:0.5",
                    "--model-price",
                    "haiku>100000=0.5:0.05:0.625:2.5",
                    "--model-price",
                    "opus=4",
                ]
            )
        prices = json.loads(out.getvalue())["model_prices"]
        self.assertEqual(sorted(prices), ["haiku", "haiku>100000", "opus"])
        self.assertEqual(prices["haiku>100000"]["input"], 0.5)

    def test_shares_are_of_weighted_tokens_without_prices(self) -> None:
        total = fr.total_row(self.build(), [], fr.PRICES)
        # Haiku: 1_000 + 1_000 (200 out x 5) + 1_000 (10_000 read x 0.1) = 3_000; Opus: 2_000.
        self.assertEqual(total["models"], {self.HAIKU: 0.6, self.OPUS: 0.4})
        self.assertIsNone(total["cost_usd"])

    def test_dollars_and_shares_of_cost_with_prices(self) -> None:
        rates = price_table("haiku=1", "opus=5")  # dollars per million fresh input tokens
        total = fr.total_row(self.build(), [], fr.PRICES, model_prices=rates)
        # Haiku 3_000 x $1/M = $0.003; Opus 2_000 x $5/M = $0.010.
        self.assertAlmostEqual(total["cost_usd"], 0.013)
        self.assertAlmostEqual(total["models"][self.HAIKU], 0.003 / 0.013)
        rows = {r["model"]: r for r in fr.model_rows(self.build(), fr.PRICES, rates)}
        self.assertEqual(list(rows), [self.OPUS, self.HAIKU])  # biggest share first
        self.assertEqual((rows[self.HAIKU]["calls"], rows[self.HAIKU]["weighted"]), (2, 3_000))
        self.assertAlmostEqual(rows[self.OPUS]["cost_usd"], 0.010)
        self.assertAlmostEqual(rows[self.OPUS]["share"], 0.010 / 0.013)

    def test_cli_shows_models_and_dollars_only_when_priced(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(["--root", str(self.root)])
        text = out.getvalue()
        self.assertIn("haiku-5-5 60%, opus-5-5 40%", text)
        self.assertIn("## Per model", text)
        self.assertNotIn("Cost $", text)
        out = io.StringIO()
        with redirect_stdout(out):
            code = fr.main(
                ["--root", str(self.root), "--model-price", "haiku=1", "--model-price", "opus=5"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Cost $", out.getvalue())
        self.assertIn("$0.01", out.getvalue())

    def test_cli_takes_four_rates_and_echoes_them_in_json(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            code = fr.main(
                [
                    "--root",
                    str(self.root),
                    "--json",
                    "--model-price",
                    "haiku=0.1",
                    "--model-price",
                    "opus=4:0.2:5:20",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["model_prices"]["opus"]["cache_read"], 0.2)
        self.assertAlmostEqual(report["model_prices"]["haiku"]["output"], 0.5)
        # Opus 2_000 input x $4 = $0.008; Haiku 1_000 x 0.1 + 10_000 x 0.01 + 200 x 0.5 = $0.0003.
        self.assertAlmostEqual(report["total"]["cost_usd"], 0.0083)

    def test_cli_refuses_to_price_some_models_and_not_others(self) -> None:
        self.build()
        err = io.StringIO()
        with redirect_stderr(err):
            code = fr.main(["--root", str(self.root), "--model-price", "haiku=1"])
        self.assertEqual(code, 2)
        self.assertIn(self.OPUS, err.getvalue())

    def test_cli_rejects_a_price_that_is_not_a_number(self) -> None:
        self.build()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fr.main(["--root", str(self.root), "--model-price", "haiku"])


class Roles(Fixture):
    HAIKU, OPUS = Models.HAIKU, Models.OPUS

    def build(self) -> list[fr.Request]:
        when = "2026-11-10T10:00:00Z"
        paths = [
            self.write(
                "p/a.jsonl", [record("a1", when, branch="issue/52", inp=3_000, model=self.HAIKU)]
            ),
            # The author's subagent ran on another model than the author: --model is not passed on.
            self.write(
                "p/a/subagents/agent-x.jsonl",
                [record("a2", when, branch="issue/52", inp=1_000, model=self.OPUS)],
            ),
            self.write(
                "p/r.jsonl",
                [prompt(REVIEW_57), record("r1", when, branch="HEAD", inp=4_000, model=self.OPUS)],
            ),
            self.write(
                "p/r/subagents/agent-y.jsonl",
                [record("r2", when, branch="HEAD", inp=2_000, model=self.HAIKU)],
            ),
        ]
        return fr.read_requests(paths)

    def test_each_call_has_a_role(self) -> None:
        roles = {r.request_id: fr.role(r) for r in self.build()}
        self.assertEqual(
            roles,
            {"a1": "author", "a2": "author subagent", "r1": "reviewer", "r2": "reviewer subagent"},
        )

    def test_rows_split_by_role_and_model_biggest_first(self) -> None:
        rows = fr.role_rows(self.build(), fr.PRICES)
        self.assertEqual(
            [(r["role"], r["model"], r["weighted"]) for r in rows],
            [
                ("reviewer", self.OPUS, 4_000),
                ("author", self.HAIKU, 3_000),
                ("reviewer subagent", self.HAIKU, 2_000),
                ("author subagent", self.OPUS, 1_000),
            ],
        )
        self.assertAlmostEqual(rows[0]["share"], 0.4)
        self.assertIsNone(rows[0]["cost_usd"])

    def test_rows_are_priced_in_dollars_when_given(self) -> None:
        rows = fr.role_rows(self.build(), fr.PRICES, price_table("haiku=1", "opus=5"))
        # Reviewer Opus 4_000 x $5/M = $0.020 of $0.020 + $0.003 + $0.002 + $0.005 = $0.030.
        self.assertEqual(rows[0]["role"], "reviewer")
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.020)
        self.assertAlmostEqual(rows[0]["share"], 0.020 / 0.030)

    def test_cli_prints_the_role_table_and_json(self) -> None:
        self.build()
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(["--root", str(self.root)])
        self.assertIn("## Per role and model", out.getvalue())
        self.assertIn("| author subagent | claude-opus-5-5 |", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            fr.main(["--root", str(self.root), "--json"])
        roles = json.loads(out.getvalue())["roles"]
        self.assertEqual(len(roles), 4)


if __name__ == "__main__":
    unittest.main()
