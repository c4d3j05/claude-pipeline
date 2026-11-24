# Design

A release counts as covered on commit C when C is at or after the release's merge on main's first-parent
history and a `success` `explorer/batch-settled/<lane>` (for that release's batch lane) exists on a
first-parent commit between the merge and C. The newest commit since the last tag where every release
merged up to it is covered is the one tagged. Releases merged before the previous tag are already covered.
A legacy `explorer/batch-settled` status is read as lane `all`, so a pre-lane batch keeps working.
