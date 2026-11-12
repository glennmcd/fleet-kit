"""Tests for scripts/fleet_report.py, on fixed transcripts written to a temp folder."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fleet_report as fr  # noqa: E402


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
        "message": {"usage": usage},
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


class Report(Fixture):
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

    def test_cli_with_no_transcripts_fails_cleanly(self) -> None:
        empty = os.path.join(self._tmp.name, "empty")
        os.mkdir(empty)
        self.assertEqual(fr.main(["--root", empty]), 1)


if __name__ == "__main__":
    unittest.main()
