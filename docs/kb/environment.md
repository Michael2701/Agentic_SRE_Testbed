# Environment gotchas

- Host: macOS with Docker Desktop (Docker 29, Compose v5), GNU Make 3.81 (old, so avoid newer make
  features), Python 3.14, **no `uv`**. Run Python tooling in containers.
- The Claude Code auto-mode classifier blocks Claude from changing git credential/remote config.
  Give the user the commands to run themselves.
- In the user's plain terminal a `! cmd` prefix is not needed; zsh just negates the exit code.
- Docker Desktop uses the containerd image store (`docker info` driver `overlayfs`), which breaks cAdvisor.
- PyPI occasionally times out during parallel image builds. pip timeouts/retries are raised in Dockerfiles;
  if it still fails, just rerun `make up`.
- Headless Chrome screenshots of Grafana hang (live refresh keeps the page busy). Verify dashboards
  by evaluating panel queries through the APIs instead.
- Don't chain `sleep` in Bash (blocked); use run_in_background or Monitor with an until-loop.
