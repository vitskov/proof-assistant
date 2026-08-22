# Start here

This ZIP contains the complete internal `repoprover-codex` project.

On the Linux server or Mac with Codex CLI already installed:

```bash
unzip repoprover-codex-handoff.zip
cd repoprover-codex
codex
```

Then tell Codex:

> Read `CODEX_HANDOFF.md` completely. Continue the implementation and execute the test plan autonomously. Fix problems you find. Do not open or prepare an upstream RepoProver pull request.

The same handoff explicitly covers Linux and macOS local-mode testing.

For the installed high-level manuscript workflow, place the requested work in a
UTF-8 file and run:

```bash
repoprover-codex manuscript-run \
  --manuscript /absolute/path/to/latex-source \
  --task-file /absolute/path/to/task.md \
  --output "$HOME/repoprover-runs/run-001" \
  --model gpt-5.6-luna \
  --effort low
```

The output directory must be new or empty and must be outside Dropbox.
