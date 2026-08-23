# Start here

Choose the guide that matches what you want to do:

- Install or upgrade: [docs/INSTALLATION.md](docs/INSTALLATION.md)
- Verify a manuscript: [docs/MANUSCRIPT_RUNS.md](docs/MANUSCRIPT_RUNS.md)
- Understand or clean disk usage:
  [docs/CACHE_AND_STORAGE.md](docs/CACHE_AND_STORAGE.md)
- Develop and test the bridge: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

The shortest installed manuscript command is:

```bash
repoprover-codex manuscript-run \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --output "$OUTPUT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

The manuscript is read-only. The output must be new or empty and outside
Dropbox. Run `repoprover-codex cache status` before a large job if you want to
see current disk headroom.
