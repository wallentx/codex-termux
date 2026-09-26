# Termux release generation

`dev` owns automation. `wallentx/termux-target` owns compatibility code.
Release code starts from the exact requested upstream tag, never from a normal
merge of the target's full product tree with that tag.

```text
paired upstream tag ---- tested Termux snapshot
                    diff          |
                      |           |
requested upstream + Termux delta |
                      |           |
                 release tree ----+--> checkpoint after CI and deployment
```

`scripts/termux-release-tree.py` performs an explicit-base three-way merge using
a private index. The baseline is the upstream tag paired with the selected
snapshot's latest reachable Termux release tag. For an older release line, the
generator selects a compatible tested snapshot instead of a newer target.

Automation and package-version metadata are excluded from the portable delta.
Upstream file renames are supported. All other upstream paths must match the
requested release exactly. Cargo members, inherited dependencies, and local
dependency manifests are checked without invoking Cargo.

The updater adapter preserves upstream daemon arguments while making its call
chain async for Termux. Only explicitly recognized Termux insertions can be
combined with adjacent upstream additions; other conflicts fail the preflight.
The historical self-update patch is no longer reapplied on every release.

The Android warning guards for `SuppressStderr` are retired when upstream
removes that helper, but only if those guards are the entire downstream change
to `clipboard_copy.rs`. Any additional clipboard changes still need merging.

Preflight completes before the generator creates, deletes, or pushes release
branches. A retry producing the same PR tree preserves its commit and running
CI. Metadata records both `patch_source_sha` and `patch_upstream_ref`; the source
snapshot remains the baseline for [checkpoints](termux-checkpoints.md).

## Git-only verification

```sh
bash scripts/test-termux-create-release-pr.sh
bash scripts/test-termux-create-checkpoint-pr.sh
python3 -B scripts/test-termux-sync-file-lock.py
```

`scripts/test-termux-release-tags.sh` additionally checks the real stable
0.155.0 and alpha.16/alpha.17 tags against the alpha.9 Termux delta, including the
actual updater and exact upstream config/features trees. Fetch those upstream
tags and `rust-v0.155.0-alpha.9-termux` first. CI runs these fixtures in
`termux-release-tests.yml`; no Rust compilation is needed.
