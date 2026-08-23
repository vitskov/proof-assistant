# Architecture and security boundary

```text
RepoProver agent
    |
    | OpenAI-style tool schemas and handle_tool_call()
    v
repoprover-codex adapter
    |
    | client-defined dynamicTools over JSONL
    v
persistent codex app-server
    |
    v
existing local Codex login / ChatGPT entitlement
```

RepoProver remains the control plane for Lean, Git, files, shell commands, and
Mathlib search. The Codex child is started with its native filesystem sandbox
read-only for RepoProver runs.

Before starting a turn, the backend enumerates configured MCP servers and local
skills, disables them in child-only configuration, disables apps, plugins,
bundled skills, and automatic skill instructions, and then checks the effective
app-server inventories. Startup fails closed if an external MCP capability or
enabled skill remains.

The isolation configuration does not change the user's persistent Codex
configuration. Authentication remains inside Codex; this package never reads
`~/.codex/auth.json` and never converts the login into an API key.

The integration is not offline. Manuscript context and dynamic-tool results
needed by the agent are processed through the authenticated Codex service.
Users remain responsible for applying their organization's confidentiality and
data-handling policy to manuscript inputs.

One persistent Codex thread is used per logical RepoProver agent. Exact model
and effort values are validated against the installed app-server's
`model/list` result and sent explicitly with the request.

The process-level concurrency default is deliberately conservative: at most
two active Codex turns in one process. The cache lease design separately makes
concurrent Lean projects safe.

## Storage control plane

```text
managed operation
    |
    v
exclusive admission lease
    |
    +--> recover dead reservations via OS leases
    +--> reconcile one SQLite row per coarse eviction unit
    +--> bounded GC if limits require it
    +--> persist conservative capacity reservation
    |
    v
shared build/global/depot leases during Lake and Codex work
    |
    v
refresh dirty rows -> release reservation -> best-effort bounded GC
```

The data plane remains plain Lake directories and symlinks. The SQLite index is
accounting metadata and can be reconstructed from coarse filesystem units. It
does not contain proof source, authentication material, or irreplaceable
evidence.
