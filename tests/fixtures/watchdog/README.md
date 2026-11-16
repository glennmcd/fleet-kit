`check_runs_rerun.json` is GitHub's reply to
`gh api "repos/glennmcd/commitment-tracker-ai/commits/2d08681/check-runs?per_page=100"`, recorded on 2026-11-15
and cut down to the fields the watchdog reads. The `drill` check failed at 02:34:56Z and a newer run of it, in
its own check suite, passed at 02:38:29Z; the reply lists both.
