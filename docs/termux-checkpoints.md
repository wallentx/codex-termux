# Termux release checkpoints

`wallentx/termux-target` owns the reusable compatibility code. `dev` owns release
and checkpoint automation. A checkpoint returns tested release code to the
compatibility branch without copying release-only automation into it.

## Merge baseline

Release PRs are squash-merged. The ordinary Git merge base can therefore predate
the target snapshot used to prepare the release, producing conflicts even when
no one changed the target during the build.

`scripts/termux-checkpoint-tree.sh` uses `patch_source_sha` from the release's
`.github/termux-release.json` as the three-way merge baseline:

```text
recorded target snapshot
       |              |
current target    tested release
       |              |
       +--- checkpoint tree
```

The helper verifies the recorded patch branch and requires the snapshot to be an
ancestor of the current target. It removes release-only differences from both
input trees before merging: `.github` and the helper paths maintained in
`scripts/termux-release-paths.sh` retain their destination versions.

This accepts tested upstream changes and compatibility fixes while preserving
independent target edits made after release preparation. It handles file moves
and non-overlapping edits using Git's normal three-way content merge.

## Conflicts and retries

Genuinely overlapping target edits still require review. The helper reports the
paths and fails before changing the checkout or publishing a checkpoint. It must
never resolve code conflicts by silently keeping the old destination version.

Clean checkpoints use merge-commit auto-merge. Drafts, GitHub-reported conflicts,
and legacy checkpoint PRs whose bodies report discarded merge conflicts have
auto-merge disabled. Repeating a completed checkpoint produces the same tree.

For a read-only preview, with the source and destination refs already fetched:

```sh
bash scripts/termux-checkpoint-tree.sh \
  <tested-release-commit> origin/wallentx/termux-target wallentx/termux-target
```

The command prints a tree ID on success. Compare it with the target using
`git diff origin/wallentx/termux-target <tree-id>` before publishing a repair.

Local regression tests do not invoke Cargo:

```sh
bash scripts/test-termux-create-checkpoint-pr.sh
bash scripts/test-termux-create-release-pr.sh
```
