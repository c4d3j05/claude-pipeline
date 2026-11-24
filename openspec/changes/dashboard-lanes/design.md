# Design

The table reads `status.lanes` as a list of `{lane, focus: {batch, settled, total} | null, last_run,
spent_today, budget, open_findings, next_run}`. Unknown keys are ignored and a missing block hides the
table, so the dashboard keeps working against either explorer version during the rollout.
