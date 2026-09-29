# Git workflow

## Branching (mandatory, set 2026-09-29, from M3 on)
1. Work branch from `develop`: `feature/m<N>-<slug>` (e.g. `feature/m3-tracing`).
2. PR `feature/...` → `develop`, then merge.
3. PR `develop` → `main`, then merge.
4. A milestone is finished only after the merge into `main`. The next milestone branches from the updated
   `develop` after that.
- Never commit directly to `main` or `develop`.
- `develop` was created from `main` at `f0fda81` (M2).

## Who does what
- Claude creates branches and commits locally when asked, and prepares PR titles/bodies.
- The user pushes with the repo-local alias `git ppush` (it switches the gh account to the personal one
  for the push). PR creation/merge also needs the personal gh account. Claude does not push or open PRs
  unless the user sets that up and asks.
- Remote `github.com/Michael2701/Agentic_SRE_Testbed`. Commit identity is set repo-locally.

## Rules
- Don't amend or rebase commits that are already on `origin` without asking. That once forced a
  force-push.
- `.idea/`, `.env`, caches are ignored (`.gitignore`).
