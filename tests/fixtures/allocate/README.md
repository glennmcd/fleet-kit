`closed_pulls.json` is GitHub's reply to
`gh api "repos/glennmcd/commitment-tracker-ai/pulls?state=closed&sort=updated&direction=desc&per_page=4"`,
recorded on 2026-11-15 and cut down to the fields the allocator reads. PR 51 merged at 18:06:28Z; the allocator
pass at 18:06:33Z gave its issue, 46, out again because the issue listing still showed it open.
