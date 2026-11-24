# Design

The default changes from squash to merge rather than reading the repository's allowed methods from
GitHub: one setting is predictable, needs no extra API call or token scope, and a repository that does
allow squash can still choose it. `merge_commit=True` (a repair) keeps forcing `--merge`, whatever the
setting, so main stays recorded as merged into the release branch.
