## 1. Tagging across lanes
- [ ] 1.1 Read `explorer/batch-settled/<lane>` contexts and the legacy one
- [ ] 1.2 Map releases merged into main up to a commit to their batch and lane (tracker `Batch:` line, `merged:` comment)
- [ ] 1.3 Tag only when every merged release is covered; notes list batches and lanes
- [ ] 1.4 `tag --dry-run` names blocking releases
- [ ] 1.5 Tests: one lane settled while another's merged release is not → no tag; both → tag; legacy status

## 2. Deploy
- [ ] 2.1 With explorer `lane-tags` live, turn `RELEASE_TAGS` back on
