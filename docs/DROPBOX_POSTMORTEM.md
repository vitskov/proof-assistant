# Dropbox work-directory incident postmortem

## Summary

The legacy output tree `/data1/homes/vui1/Dropbox/work/laplacians` was created on 2026-08-22. It contains generated `artifacts/`, a copied Lean `workspace/`, and a real `workspace/.lake` directory. This is historical output; it was not produced by the current guarded workflow.

## Root cause

Saved metadata identifies the run as `manuscript-run`, with its working and output root under `/Users/vui1/repoprover-runs/laplacians` on macOS. That evidence contradicts a claim that this particular run directly wrote to Dropbox. The generated tree was later present at `/data1/homes/vui1/Dropbox/work/laplacians`; whether it was moved, copied, or synchronized there is not recorded. The exact relocation mechanism and command line are unknown.

The incident therefore cannot be attributed conclusively to a direct Proof Assistant Dropbox write. The safe lesson is that every API and output boundary must enforce the same prohibition, including legacy entry points and exports, so relocation or caller mistakes cannot turn generated state into an active Dropbox workspace.

## Contract and prevention

Dropbox manuscript/source directories are read-only inputs. Proof Assistant must never write projects, workspaces, `.lake`, reports, logs, caches, temporary files, or environments there. Any requested Dropbox work/output directory is rejected with a user-facing “prohibited by design” error.

Enforcement is defense-in-depth: managed project destinations and every incremental session reject Dropbox; workspace preparation validates before staging; explicit graph exports validate their output path; cache, temporary, provider, and concurrency roots have independent local-only checks; and regression tests cover Dropbox rejection while preserving source-read behavior.

## Recovery

The historical tree is intentionally left untouched. Do not delete or move it automatically; archive or remove it only after an explicit user request. Fresh runs should use a local destination such as `$HOME/proof-assistant` and may continue to read a manuscript stored in Dropbox.
