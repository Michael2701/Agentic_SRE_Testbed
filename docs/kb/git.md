# Git workflow

- Remote `github.com/Michael2701/Agentic_SRE_Testbed`, branch `main`. Commit identity is set repo-locally.
- The user pushes with the repo-local alias `git ppush`, which switches the gh account for the push.
  Claude commits only when asked and never pushes.
- Don't amend or rebase commits that are already on `origin` without asking. That once forced a
  force-push.
- `.idea/`, `.env`, caches are ignored (`.gitignore`).
