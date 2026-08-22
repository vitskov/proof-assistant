from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .backend import CodexBackend, CodexConfig
from .environment import (
    EnvironmentCheckError,
    configure_lean_runtime,
    default_lean_memory_limit_gb,
)
from .models import model_id, supported_efforts


def _backend(args) -> CodexBackend:
    return CodexBackend(
        CodexConfig(
            executable=args.codex,
            model=getattr(args, "model", "") or "",
            effort=getattr(args, "effort", "high") or "high",
        ),
        cwd=Path.cwd(),
    )


def cmd_doctor(args) -> int:
    path = shutil.which(args.codex)
    if not path:
        print(f"ERROR: {args.codex!r} is not on PATH", file=sys.stderr)
        return 2
    print(f"codex executable: {path}")
    backend = _backend(args)
    try:
        backend.client.start()
        info = backend.initialize()
        print("app-server initialize: OK")
        catalog = backend.model_catalog()
        print(f"models visible to Codex: {len(catalog)}")
        if catalog:
            print("default/first models:")
            for entry in catalog[:10]:
                print(
                    f"  {model_id(entry)} "
                    f"[{', '.join(supported_efforts(entry)) or 'effort metadata unavailable'}]"
                )
        print("authentication/backend status: app-server responded successfully")
        print(
            "NOTE: this package did not inspect OAuth tokens or OPENAI_API_KEY."
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        backend.close()


def cmd_models(args) -> int:
    backend = _backend(args)
    try:
        backend.client.start()
        backend.initialize()
        catalog = backend.model_catalog()
        for entry in catalog:
            print(
                f"{model_id(entry)}\t"
                f"{','.join(supported_efforts(entry)) or '-'}"
            )
        return 0
    finally:
        backend.close()


def cmd_smoke(args) -> int:
    tool = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo a supplied string. Use this tool exactly once.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }
    seen = []

    def handler(name, arguments):
        seen.append((name, arguments))
        if name != "echo":
            return f"Error: unknown tool {name}"
        return f"ECHO:{arguments.get('text', '')}"

    backend = _backend(args)
    try:
        result = backend.run(
            system_prompt=(
                "You are testing a tool bridge. You must call the echo tool "
                "exactly once, then state briefly that the bridge succeeded."
            ),
            user_prompt='Call echo with text "repoprover-codex-smoke".',
            tools=[tool],
            tool_handler=handler,
        )
    finally:
        backend.close()

    if not seen:
        print("ERROR: Codex completed without invoking the dynamic echo tool", file=sys.stderr)
        return 3
    print(f"tool calls: {json.dumps(seen)}")
    print(f"final: {result.final_text}")
    print(f"thread: {result.thread_id}")
    print(f"turn: {result.turn_id}")
    return 0


def cmd_compiler_check(_args) -> int:
    try:
        check = configure_lean_runtime()
    except EnvironmentCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    role = "Lean native compiler" if check.lean_compiler else "native compiler"
    print(f"{role}: {check.executable}")
    print("compile/run smoke check: OK")
    if check.fallback_used:
        print("Lean bundled compiler failed; configured working fallback via LEAN_CC")
    return 0


_DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*(?:theorem|lemma)\s+(?P<name>[^\s:{(]+)"
)


def _target_declaration(source: str, theorem_name: str) -> str | None:
    matches = list(_DECLARATION_RE.finditer(source))
    for index, match in enumerate(matches):
        if match.group("name") != theorem_name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        return source[match.start() : end]
    return None


def _verify_repoprover_proof(agent, wrapped, lean_path: Path, theorem_name: str) -> tuple[str, str]:
    """Return a semantic outcome and supporting detail for one PROVE run."""
    calls = wrapped.codex.tool_calls
    if not calls:
        return "unproved", "Codex completed without invoking a RepoProver tool"

    lean_calls = [call for call in calls if call.name == "lean_check"]
    if not lean_calls:
        return "unproved", "Codex did not invoke RepoProver's lean_check tool"
    if not any(call.success for call in lean_calls):
        return "tool_failure", "all Codex-requested lean_check calls failed"

    if not lean_path.is_file():
        return "formalization_mismatch", f"Lean target does not exist: {lean_path}"
    source = lean_path.read_text(encoding="utf-8")
    declaration = _target_declaration(source, theorem_name)
    if declaration is None:
        return (
            "formalization_mismatch",
            f"theorem {theorem_name!r} is not present in {lean_path}",
        )
    if re.search(r"\b(?:sorry|axiom)\b", declaration):
        return "unproved", f"theorem {theorem_name!r} still contains sorry/axiom"

    verification = agent.handle_tool_call("lean_check", {"code": source})
    if verification.lstrip().startswith("Error:"):
        return "tool_failure", f"final RepoProver lean_check failed: {verification}"
    return "proved", verification



def cmd_repoprover_prove(args) -> int:
    """Run one RepoProver PROVE agent through the Codex backend."""
    try:
        from repoprover.agents.contributor import ContributorAgent, ContributorTask
        from repoprover.agents.lean_tools import (
            configure_global_pool,
            shutdown_global_pool,
        )
    except ImportError as exc:
        print(
            "ERROR: RepoProver is not importable. Install a RepoProver checkout "
            "into this environment first.",
            file=sys.stderr,
        )
        print(f"DETAIL: {exc}", file=sys.stderr)
        return 2

    project = Path(args.project).resolve()
    if not project.exists():
        print(f"ERROR: project does not exist: {project}", file=sys.stderr)
        return 2

    if args.lean_cc:
        os.environ["LEAN_CC"] = args.lean_cc
    try:
        compiler = configure_lean_runtime()
    except EnvironmentCheckError as exc:
        print(f"OUTCOME: tool_failure", file=sys.stderr)
        print(f"ERROR: Lean compiler preflight failed: {exc}", file=sys.stderr)
        return 5
    print(f"Lean native compiler: {compiler.executable}", file=sys.stderr)

    task = ContributorTask.prove(
        chapter_id=args.chapter,
        theorem_name=args.theorem,
        lean_path=args.lean_path or "",
        source_tex_path=args.source_tex or "",
    )
    agent = ContributorAgent(task=task, repo_root=project)

    # RepoProver's lean_check uses a global REPL pool; configure a deliberately
    # small pool for internal single-agent testing.
    memory_limit = (
        args.lean_memory_limit_gb
        if args.lean_memory_limit_gb is not None
        else default_lean_memory_limit_gb()
    )
    configure_global_pool(
        project,
        pool_size=args.lean_pool_size,
        instance_mem_limit_gb=memory_limit,
    )
    try:
        from .integration import run_repoprover_agent

        try:
            wrapped = run_repoprover_agent(
                agent,
                run_kwargs={},
                codex=CodexConfig(
                    executable=args.codex,
                    model=args.model,
                    effort=args.effort,
                    request_timeout=args.request_timeout,
                    turn_timeout=args.turn_timeout,
                    sandbox="read-only",
                ),
            )
        except Exception as exc:
            print("OUTCOME: provider_failure", file=sys.stderr)
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        lean_path = project / (args.lean_path or "")
        try:
            outcome, detail = _verify_repoprover_proof(
                agent, wrapped, lean_path, args.theorem
            )
        except Exception as exc:
            print("OUTCOME: tool_failure", file=sys.stderr)
            print(f"ERROR: final Lean verification failed: {exc}", file=sys.stderr)
            return 5
    finally:
        shutdown_global_pool()

    print(wrapped.codex.final_text)
    print(f"\nthread={wrapped.codex.thread_id}")
    print(f"turn={wrapped.codex.turn_id}")
    print(f"tool_calls={len(wrapped.codex.tool_calls)}")
    print(f"tool_names={','.join(call.name for call in wrapped.codex.tool_calls)}")
    print(f"OUTCOME: {outcome}")
    print(f"verification: {detail}")
    return {
        "proved": 0,
        "unproved": 4,
        "tool_failure": 5,
        "formalization_mismatch": 6,
    }[outcome]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repoprover-codex")
    p.add_argument("--codex", default="codex", help="Codex executable")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check Codex app-server connectivity")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("models", help="List models/efforts advertised by Codex")
    m.set_defaults(func=cmd_models)

    cc = sub.add_parser(
        "compiler-check", help="Compile and run a native program for Lean/Lake"
    )
    cc.set_defaults(func=cmd_compiler_check)

    s = sub.add_parser("smoke", help="Run a real dynamic-tool smoke test")
    s.add_argument("--model", required=True)
    s.add_argument("--effort", default="high")
    s.set_defaults(func=cmd_smoke)

    rp = sub.add_parser(
        "repoprover-prove",
        help="Run one real RepoProver PROVE agent through Codex",
    )
    rp.add_argument("--project", required=True, help="Lean/RepoProver project root")
    rp.add_argument("--chapter", required=True)
    rp.add_argument("--theorem", required=True)
    rp.add_argument("--lean-path", default="")
    rp.add_argument("--source-tex", default="")
    rp.add_argument("--model", required=True)
    rp.add_argument("--effort", default="high")
    rp.add_argument("--lean-pool-size", type=int, default=1)
    rp.add_argument(
        "--lean-memory-limit-gb",
        type=int,
        default=None,
        help="Per-REPL address-space limit (default: 0 on macOS, 24 on Linux)",
    )
    rp.add_argument(
        "--lean-cc",
        default=None,
        help="Native compiler for Lake (auto-detected when omitted)",
    )
    rp.add_argument("--request-timeout", type=float, default=120.0)
    rp.add_argument("--turn-timeout", type=float, default=1800.0)
    rp.set_defaults(func=cmd_repoprover_prove)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
