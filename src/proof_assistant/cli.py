from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .backend import CodexBackend, CodexConfig
from .cache import (
    COLD_DEPOT_RESERVE_GB,
    WARM_PROJECT_RESERVE_GB,
    CacheCapacityError,
    CacheLayout,
    CacheLocationError,
    attach_project_cache,
    cache_policy,
    cache_usage,
    claim_dependency_depot,
    dependency_cache_key,
    dependency_depot_ready,
    dependency_depot_target,
    ensure_project_cache_managed,
    ensure_project_outside_dropbox,
    garbage_collect_cache,
    initialize_cache,
    managed_project_session,
)
from .environment import (
    EnvironmentCheckError,
    configure_lean_runtime,
    default_lean_memory_limit_gb,
    select_native_compiler,
)
from .models import model_id, supported_efforts

_ACTIVE_CLEANUPS: list[Callable[[], None]] = []


def _hold_cleanup(callback: Callable[[], None]) -> None:
    _ACTIVE_CLEANUPS.append(callback)


def _release_active_resources() -> None:
    while _ACTIVE_CLEANUPS:
        callback = _ACTIVE_CLEANUPS.pop()
        try:
            callback()
        except Exception as exc:
            print(f"WARNING: cache cleanup failed: {exc}", file=sys.stderr)


def _format_gib(value: int) -> str:
    return f"{value / (1024**3):.2f} GiB"


def _run_status_is_terminal(path: Path) -> bool:
    """Prevent deferred cleanup progress from replacing a finished outcome."""
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("outcome") not in {None, "running"}


def _write_manuscript_failure(
    paths,
    *,
    started_at: str,
    outcome: str,
    detail: str,
    exit_code: int,
) -> int:
    from .manuscript import utc_now, write_json

    payload = {
        "schema_version": 1,
        "command": "manuscript-run",
        "started_at": started_at,
        "completed_at": utc_now(),
        "outcome": outcome,
        "detail": detail,
        "exit_code": exit_code,
        "output": str(paths.output),
        "workspace": str(paths.workspace),
    }
    write_json(paths.artifacts / "result.json", payload)
    write_json(paths.output / "RUN_STATUS.json", payload)
    (paths.artifacts / "error.txt").write_text(detail + "\n", encoding="utf-8")
    print(f"OUTCOME: {outcome}", file=sys.stderr)
    print(f"ERROR: {detail}", file=sys.stderr)
    print(f"artifacts: {paths.artifacts}", file=sys.stderr)
    return exit_code


def _cache_layout(args) -> CacheLayout:
    return CacheLayout.discover(getattr(args, "cache_home", None))


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
        backend.initialize()
        print("app-server initialize: OK")
        catalog = backend.model_catalog()
        print(f"models visible to Codex: {len(catalog)}")
        if catalog:
            print("default/first models:")
            for entry in catalog[:10]:
                efforts = (
                    ", ".join(supported_efforts(entry)) or "effort metadata unavailable"
                )
                print(f"  {model_id(entry)} [{efforts}]")
        print("authentication/backend status: app-server responded successfully")
        print("NOTE: this package did not inspect OAuth tokens or OPENAI_API_KEY.")
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
            print(f"{model_id(entry)}\t{','.join(supported_efforts(entry)) or '-'}")
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
            user_prompt='Call echo with text "proof-assistant-smoke".',
            tools=[tool],
            tool_handler=handler,
        )
    finally:
        backend.close()

    if not seen:
        print(
            "ERROR: Codex completed without invoking the dynamic echo tool",
            file=sys.stderr,
        )
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


def cmd_cache_path(args) -> int:
    try:
        layout = _cache_layout(args)
    except CacheLocationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(layout.root)
    return 0


def cmd_cache_init(args) -> int:
    try:
        layout = _cache_layout(args)
        config, check = initialize_cache(
            layout,
            max_gb=getattr(args, "max_gb", None),
            min_free_gb=getattr(args, "min_free_gb", None),
        )
    except (CacheLocationError, EnvironmentCheckError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"cache root: {layout.root}")
    print(f"filesystem: {layout.filesystem_type}")
    print(f"Mathlib downloads: {layout.mathlib_downloads}")
    print(f"Lake system cache: {layout.lake_system}")
    print(f"dependency depots: {layout.lake_dependencies}")
    print(f"isolated builds: {layout.lake_builds}")
    print(f"worktrees: {layout.worktrees}")
    print(f"fixtures: {layout.fixtures}")
    print(f"native compiler: {check.executable}")
    print(f"cache limit: {_format_gib(config.max_bytes)}")
    print(f"minimum free space: {_format_gib(config.min_free_bytes)}")
    print(f"configuration: {layout.config_path}")
    if config.compiler_fallback_used:
        print("Lean bundled compiler failed; recorded working fallback")
    return 0


def cmd_cache_doctor(args) -> int:
    try:
        layout = _cache_layout(args)
        missing = [path for path in layout.directories if not path.is_dir()]
        if missing:
            raise CacheLocationError(
                "Cache is not initialized; run `proof-assistant cache init`"
            )
        config = layout.load_config()
        if config is None:
            raise CacheLocationError(
                "Cache compiler configuration is missing; run "
                "`proof-assistant cache init`"
            )
        runtime = layout.runtime_environment(lean_cc=config.lean_cc)
        check = select_native_compiler(runtime)
        policy = cache_policy(config)
        usage = cache_usage(layout)
    except (CacheLocationError, EnvironmentCheckError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"cache root: {layout.root}")
    print(f"filesystem: {layout.filesystem_type} (local)")
    print("inside user home: yes")
    print("inside Dropbox: no")
    print(f"native compiler: {check.executable}")
    print("compile/run smoke check: OK")
    print(f"managed cache: {_format_gib(usage.managed_bytes)}")
    print(f"cache limit: {_format_gib(policy.max_bytes)}")
    print(f"filesystem free: {_format_gib(usage.free_bytes)}")
    print(f"minimum free space: {_format_gib(policy.min_free_bytes)}")
    print(f"active reservations: {_format_gib(usage.reserved_bytes)}")
    return 0


def cmd_cache_attach(args) -> int:
    try:
        layout = _cache_layout(args)
        target = attach_project_cache(args.project, layout)
    except (CacheLocationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"project: {Path(args.project).expanduser().resolve()}")
    print(f"managed .lake: {target}")
    return 0


def cmd_cache_status(args) -> int:
    try:
        layout = _cache_layout(args)
        layout.create()
        config = layout.load_config()
        policy = cache_policy(config)
        usage = cache_usage(layout)
    except CacheLocationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"cache root: {layout.root}")
    print(f"managed cache: {_format_gib(usage.managed_bytes)}")
    print(f"cache limit: {_format_gib(policy.max_bytes)}")
    print(f"filesystem free: {_format_gib(usage.free_bytes)}")
    print(f"minimum free space: {_format_gib(policy.min_free_bytes)}")
    print(f"dependency depots: {_format_gib(usage.dependency_bytes)}")
    print(f"isolated project builds: {_format_gib(usage.build_bytes)}")
    print(f"Mathlib downloads: {_format_gib(usage.download_bytes)}")
    print(f"Lake system cache: {_format_gib(usage.lake_system_bytes)}")
    print(f"temporary files: {_format_gib(usage.temporary_bytes)}")
    print(f"active reservations: {_format_gib(usage.reserved_bytes)}")
    print(f"accounting index: {layout.index_path}")
    return 0


def cmd_cache_gc(args) -> int:
    try:
        layout = _cache_layout(args)
        layout.create()
        policy = cache_policy(layout.load_config())
        result = garbage_collect_cache(
            layout,
            policy,
            strict=False,
            timeout=args.gc_timeout,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except CacheLocationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"before: {_format_gib(result.before.managed_bytes)}")
    print(f"after: {_format_gib(result.after.managed_bytes)}")
    print(f"filesystem free: {_format_gib(result.after.free_bytes)}")
    print(f"removed entries: {len(result.removed)}")
    print(f"recursive measurements: {result.recursive_measurements}")
    for item in result.removed:
        print(f"  removed {item}")
    for item in result.skipped_active:
        print(f"  active {item}")
    return 0


def cmd_cache_prepare(args) -> int:
    """Prepare and validate one Lean project without starting Codex."""
    from .manuscript import bootstrap_lean_workspace, command_records_text

    project = Path(args.project).expanduser().resolve()
    claim = None
    session = None
    try:
        layout = _cache_layout(args)
        layout.create()
        config = layout.load_config()
        if config is None:
            config, _check = initialize_cache(layout)
        if args.lean_cc:
            os.environ["LEAN_CC"] = args.lean_cc
        compiler = configure_lean_runtime()
        if config.lean_cc != compiler.executable:
            config = layout.record_compiler(compiler)
        runtime_env = layout.runtime_environment(
            os.environ, lean_cc=compiler.executable
        )
        key = dependency_cache_key(project, env=runtime_env)
        depot = dependency_depot_target(layout, key)
        reserve = (
            WARM_PROJECT_RESERVE_GB
            if dependency_depot_ready(depot)
            else COLD_DEPOT_RESERVE_GB
        )
        session = managed_project_session(
            project,
            layout,
            cache_policy(config),
            attach=True,
            reserve_gb=reserve,
            lease_timeout=args.setup_timeout,
            gc_timeout=args.gc_timeout,
            progress=lambda message: print(message, file=sys.stderr),
        )
        managed_lake = session.__enter__()
        claim = claim_dependency_depot(
            project, layout, env=runtime_env, timeout=args.setup_timeout
        )
        was_ready = claim.ready
        records = bootstrap_lean_workspace(
            project,
            env=runtime_env,
            timeout=args.setup_timeout,
            depot_claim=claim,
        )
        failed = next(
            (record for record in records if record.required and not record.succeeded),
            None,
        )
        if failed is not None:
            print(command_records_text(records), file=sys.stderr)
            print(
                f"ERROR: required setup command failed: {' '.join(failed.argv)}",
                file=sys.stderr,
            )
            return 5
        print(f"project: {project}")
        print(f"managed .lake: {managed_lake}")
        print(f"dependency key: {claim.key}")
        print(f"dependency depot: {claim.target}")
        print(f"dependency depot reused: {'yes' if was_ready else 'no'}")
        print(f"native compiler: {compiler.executable}")
        print("lake build: OK")
        return 0
    except (
        CacheLocationError,
        CacheCapacityError,
        EnvironmentCheckError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if claim is not None:
            claim.close()
        if session is not None:
            session.__exit__(None, None, None)


_DECLARATION_RE = re.compile(r"(?m)^[ \t]*(?:theorem|lemma)\s+(?P<name>[^\s:{(]+)")


def _target_declaration(source: str, theorem_name: str) -> str | None:
    matches = list(_DECLARATION_RE.finditer(source))
    for index, match in enumerate(matches):
        if match.group("name") != theorem_name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        return source[match.start() : end]
    return None


def _verify_repoprover_proof(
    agent, wrapped, lean_path: Path, theorem_name: str
) -> tuple[str, str]:
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

    try:
        cache_layout = _cache_layout(args)
        cache_layout.create()
        cache_config = cache_layout.load_config()
        cache_layout.apply_runtime_environment(
            lean_cc=cache_config.lean_cc if cache_config else None
        )
        ensure_project_cache_managed(project, cache_layout)
    except CacheLocationError as exc:
        print("OUTCOME: tool_failure", file=sys.stderr)
        print(f"ERROR: cache policy check failed: {exc}", file=sys.stderr)
        return 5

    if args.lean_cc:
        os.environ["LEAN_CC"] = args.lean_cc
    try:
        compiler = configure_lean_runtime()
    except EnvironmentCheckError as exc:
        print("OUTCOME: tool_failure", file=sys.stderr)
        print(f"ERROR: Lean compiler preflight failed: {exc}", file=sys.stderr)
        return 5
    if cache_config is None or cache_config.lean_cc != compiler.executable:
        cache_layout.record_compiler(compiler)
    runtime_env = cache_layout.runtime_environment(
        os.environ, lean_cc=compiler.executable
    )
    dependency_target = dependency_depot_target(
        cache_layout,
        dependency_cache_key(project, env=runtime_env),
    )
    if not dependency_depot_ready(dependency_target):
        print("OUTCOME: tool_failure", file=sys.stderr)
        print(
            "ERROR: dependency depot is not prepared; run "
            f"`proof-assistant cache prepare --project {project}` first",
            file=sys.stderr,
        )
        return 5
    try:
        cache_session = managed_project_session(
            project,
            cache_layout,
            cache_policy(cache_layout.load_config()),
            attach=False,
            reserve_gb=WARM_PROJECT_RESERVE_GB,
            lease_timeout=args.request_timeout,
            gc_timeout=args.gc_timeout,
            progress=lambda message: print(message, file=sys.stderr),
        )
        cache_session.__enter__()
        _hold_cleanup(lambda session=cache_session: session.__exit__(None, None, None))
        depot_claim = claim_dependency_depot(
            project,
            cache_layout,
            env=runtime_env,
            timeout=args.request_timeout,
        )
        _hold_cleanup(depot_claim.close)
    except CacheLocationError as exc:
        print("OUTCOME: tool_failure", file=sys.stderr)
        print(f"ERROR: cache capacity/lease check failed: {exc}", file=sys.stderr)
        return 5
    print(f"cache root: {cache_layout.root}", file=sys.stderr)
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


def cmd_manuscript_run(args) -> int:
    """Run a free-form, file-specified manuscript verification task."""
    from .manuscript import (
        ManuscriptInputError,
        bootstrap_lean_workspace,
        command_records_text,
        commit_bootstrap_state,
        create_manuscript_agent,
        evaluate_manuscript_run,
        prepare_manuscript_workspace,
        run_command,
        serialize_command,
        serialize_tool_call,
        utc_now,
        workspace_git_state,
        write_json,
    )

    started_at = utc_now()
    output = Path(args.output).expanduser().resolve()
    try:
        cache_layout = _cache_layout(args)
        # Validate before copying potentially large input trees. The workspace
        # is a Lean project even when the source directory only contains TeX.
        ensure_project_outside_dropbox(output / "workspace", cache_layout)
        paths = prepare_manuscript_workspace(
            args.manuscript,
            output,
            args.task_file,
        )
    except (CacheLocationError, ManuscriptInputError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"output: {paths.output}")
    print(f"workspace: {paths.workspace}")
    print(f"input mode: {paths.source_mode}")
    print(f"LaTeX sources: {len(paths.latex_sources)}")

    def record_phase(phase: str, detail: str) -> None:
        status_path = paths.output / "RUN_STATUS.json"
        if _run_status_is_terminal(status_path):
            return
        payload = {
            "schema_version": 1,
            "command": "manuscript-run",
            "started_at": started_at,
            "outcome": "running",
            "phase": phase,
            "detail": detail,
            "output": str(paths.output),
            "workspace": str(paths.workspace),
        }
        write_json(status_path, payload)

    def cache_progress(message: str) -> None:
        print(message, file=sys.stderr)
        record_phase("cache_gc", message)

    record_phase("cache_preflight", "Validating cache capacity and reservations")

    try:
        cache_layout.create()
        cache_config = cache_layout.load_config()
        cache_layout.apply_runtime_environment(
            lean_cc=cache_config.lean_cc if cache_config else None
        )

        if args.lean_cc:
            os.environ["LEAN_CC"] = args.lean_cc
        compiler = configure_lean_runtime()
        if cache_config is None or cache_config.lean_cc != compiler.executable:
            cache_layout.record_compiler(compiler)
        runtime_env = cache_layout.runtime_environment(
            os.environ,
            lean_cc=compiler.executable,
        )
        policy = cache_policy(cache_layout.load_config())
        dependency_key = dependency_cache_key(paths.workspace, env=runtime_env)
        dependency_target = dependency_depot_target(cache_layout, dependency_key)
        dependency_was_ready = dependency_depot_ready(dependency_target)
        reserve_gb = (
            WARM_PROJECT_RESERVE_GB if dependency_was_ready else COLD_DEPOT_RESERVE_GB
        )
        cache_session = managed_project_session(
            paths.workspace,
            cache_layout,
            policy,
            attach=True,
            reserve_gb=reserve_gb,
            lease_timeout=args.setup_timeout,
            gc_timeout=args.gc_timeout,
            progress=cache_progress,
        )
        managed_lake = cache_session.__enter__()
        _hold_cleanup(lambda session=cache_session: session.__exit__(None, None, None))
        depot_claim = claim_dependency_depot(
            paths.workspace,
            cache_layout,
            env=runtime_env,
            timeout=args.setup_timeout,
        )
        _hold_cleanup(depot_claim.close)
    except (CacheLocationError, EnvironmentCheckError, OSError) as exc:
        return _write_manuscript_failure(
            paths,
            started_at=started_at,
            outcome="setup_failure",
            detail=f"Lean/cache preflight failed: {exc}",
            exit_code=5,
        )

    print(f"managed .lake: {managed_lake}", file=sys.stderr)
    print(f"dependency depot: {depot_claim.target}", file=sys.stderr)
    print(
        f"dependency depot reused: {'yes' if dependency_was_ready else 'no'}",
        file=sys.stderr,
    )
    print(f"Lean native compiler: {compiler.executable}", file=sys.stderr)
    record_phase("dependency_setup", "Preparing Lean dependencies and root build")

    try:
        setup_records = bootstrap_lean_workspace(
            paths.workspace,
            env=runtime_env,
            timeout=args.setup_timeout,
            depot_claim=depot_claim,
        )
    except (CacheLocationError, OSError) as exc:
        return _write_manuscript_failure(
            paths,
            started_at=started_at,
            outcome="setup_failure",
            detail=f"Dependency depot preparation failed: {exc}",
            exit_code=5,
        )
    (paths.artifacts / "setup.log").write_text(
        command_records_text(setup_records), encoding="utf-8"
    )
    write_json(
        paths.artifacts / "setup.json",
        [serialize_command(record) for record in setup_records],
    )
    failed_setup = next(
        (
            record
            for record in setup_records
            if record.required and not record.succeeded
        ),
        None,
    )
    if failed_setup is not None:
        return _write_manuscript_failure(
            paths,
            started_at=started_at,
            outcome="setup_failure",
            detail=(
                f"Required setup command failed ({' '.join(failed_setup.argv)}); "
                f"see {paths.artifacts / 'setup.log'}"
            ),
            exit_code=5,
        )

    try:
        run_baseline = commit_bootstrap_state(paths.workspace)
        agent = create_manuscript_agent(paths.workspace)
        from repoprover.agents.lean_tools import (
            configure_global_pool,
            shutdown_global_pool,
        )
    except (ImportError, ManuscriptInputError, OSError) as exc:
        return _write_manuscript_failure(
            paths,
            started_at=started_at,
            outcome="setup_failure",
            detail=f"RepoProver agent setup failed: {exc}",
            exit_code=5,
        )

    memory_limit = (
        args.lean_memory_limit_gb
        if args.lean_memory_limit_gb is not None
        else default_lean_memory_limit_gb()
    )
    configure_global_pool(
        paths.workspace,
        pool_size=args.lean_pool_size,
        instance_mem_limit_gb=memory_limit,
    )
    record_phase("codex_turn", "RepoProver agent is running through Codex")
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
            return _write_manuscript_failure(
                paths,
                started_at=started_at,
                outcome="provider_failure",
                detail=f"{type(exc).__name__}: {exc}",
                exit_code=1,
            )
    finally:
        shutdown_global_pool()

    (paths.artifacts / "final.md").write_text(
        wrapped.codex.final_text.rstrip() + "\n", encoding="utf-8"
    )
    write_json(paths.artifacts / "events.json", wrapped.codex.events)
    write_json(
        paths.artifacts / "tool-calls.json",
        [serialize_tool_call(call) for call in wrapped.codex.tool_calls],
    )

    independent_build = run_command(
        ("lake", "build"),
        cwd=paths.workspace,
        env=runtime_env,
        timeout=args.setup_timeout,
    )
    (paths.artifacts / "verification-build.log").write_text(
        command_records_text([independent_build]), encoding="utf-8"
    )

    try:
        final_commit, git_status = workspace_git_state(paths.workspace)
    except ManuscriptInputError as exc:
        return _write_manuscript_failure(
            paths,
            started_at=started_at,
            outcome="tool_failure",
            detail=f"Could not inspect final Git state: {exc}",
            exit_code=5,
        )

    evaluation = evaluate_manuscript_run(
        final_text=wrapped.codex.final_text,
        tool_calls=wrapped.codex.tool_calls,
        report=paths.report,
        baseline_commit=run_baseline,
        final_commit=final_commit,
        git_status=git_status,
        independent_build=independent_build,
    )
    if paths.report.is_file():
        shutil.copy2(paths.report, paths.output / "VERIFICATION_REPORT.md")

    result_payload = {
        "schema_version": 1,
        "command": "manuscript-run",
        "started_at": started_at,
        "completed_at": utc_now(),
        "outcome": evaluation.outcome,
        "detail": evaluation.detail,
        "exit_code": evaluation.exit_code,
        "input": {
            "manuscript": str(paths.source_root),
            "task_file": str(Path(args.task_file).expanduser().resolve()),
            "task_sha256": paths.task_sha256,
            "source_mode": paths.source_mode,
            "latex_sources": list(paths.latex_sources),
        },
        "output": {
            "root": str(paths.output),
            "workspace": str(paths.workspace),
            "artifacts": str(paths.artifacts),
            "report": (
                str(paths.output / "VERIFICATION_REPORT.md")
                if paths.report.is_file()
                else None
            ),
            "managed_lake": str(managed_lake),
            "dependency_depot": str(depot_claim.target),
            "dependency_key": depot_claim.key,
            "dependency_reused": dependency_was_ready,
        },
        "codex": {
            "model": wrapped.codex.model,
            "effort": wrapped.codex.effort,
            "thread_id": wrapped.codex.thread_id,
            "turn_id": wrapped.codex.turn_id,
            "tool_calls": len(wrapped.codex.tool_calls),
        },
        "lean": {
            "native_compiler": compiler.executable,
            "pool_size": args.lean_pool_size,
            "memory_limit_gb": memory_limit,
            "independent_build": serialize_command(independent_build),
        },
        "git": {
            "input_commit": paths.baseline_commit,
            "run_baseline_commit": run_baseline,
            "final_commit": final_commit,
            "status_porcelain": git_status,
        },
        "evaluation": asdict(evaluation),
    }
    write_json(paths.artifacts / "result.json", result_payload)
    write_json(paths.output / "RUN_STATUS.json", result_payload)

    print(wrapped.codex.final_text)
    print(f"\nthread={wrapped.codex.thread_id}")
    print(f"turn={wrapped.codex.turn_id}")
    print(f"tool_calls={len(wrapped.codex.tool_calls)}")
    print(f"OUTCOME: {evaluation.outcome}")
    print(f"verification: {evaluation.detail}")
    print(f"output: {paths.output}")
    print(f"artifacts: {paths.artifacts}")
    return evaluation.exit_code


def cmd_manuscript_init(args) -> int:
    from .incremental.session import IncrementalSession

    project = Path(args.project).expanduser().resolve()
    try:
        layout = _cache_layout(args)
        ensure_project_outside_dropbox(project, layout)
        session = IncrementalSession.initialize(
            manuscript=args.manuscript,
            project=project,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    status = session.status()
    print(f"project: {session.project}")
    print(f"snapshot: {status['snapshot']}")
    print(f"indexed claims: {sum(status['claim_states'].values())}")
    print("status: initialized")
    return 0


def cmd_manuscript_verify(args) -> int:
    from .incremental.orchestration import VerifyOptions, verify_project
    from .incremental.session import IncrementalSession

    session = IncrementalSession(Path(args.project))
    try:
        result = verify_project(
            session,
            options=VerifyOptions(
                model=args.model,
                effort=args.effort,
                codex=args.codex,
                cache_home=args.cache_home,
                jobs=args.jobs,
                batch_size=args.batch_size,
                lean_pool_size=args.lean_pool_size,
                lean_memory_limit_gb=args.lean_memory_limit_gb,
                setup_timeout=args.setup_timeout,
                request_timeout=args.request_timeout,
                turn_timeout=args.turn_timeout,
                gc_timeout=args.gc_timeout,
            ),
        )
    except Exception as exc:
        print("OUTCOME: setup_failure", file=sys.stderr)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 20
    print(f"project: {result.project}")
    print(f"run: {result.run_id}")
    print(f"snapshot: {result.snapshot}")
    print(f"certified this run: {len(result.certified)}")
    print(f"certificates reused: {len(result.reused)}")
    print(f"statements reconciled: {len(result.reconciled)}")
    print(f"questions: {','.join(result.questions) if result.questions else 'none'}")
    print(f"OUTCOME: {result.outcome}")
    print(result.detail)
    return result.exit_code


def cmd_manuscript_status(args) -> int:
    from .incremental.session import IncrementalSession

    try:
        status = IncrementalSession(Path(args.project)).status()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True, default=str))
        return 0
    print(f"project: {status['project']}")
    print(f"mutation in progress: {'yes' if status['mutation_in_progress'] else 'no'}")
    print(f"snapshot: {status['snapshot'] or 'none'}")
    latest = status["latest_run"]
    if latest:
        print(
            f"latest run: {latest['run_id']} ({latest['outcome'] or latest['status']})"
        )
        print(f"detail: {latest['detail'] or 'none'}")
    print(f"certificates: {status['certificates']}")
    for state, count in sorted(status["claim_states"].items()):
        print(f"{state}: {count}")
    print(f"open questions: {len(status['open_questions'])}")
    return 0


def _incremental_objects_and_edges(session):
    from .incremental.models import ManuscriptEdge, SourceObject
    from .incremental.store import StateStore

    with StateStore(session.database_path) as store:
        snapshot = store.previous_snapshot()
        if snapshot is None:
            return (), ()
        objects = tuple(
            SourceObject(
                claim_id=str(row["claim_id"]),
                kind=str(row["kind"]),
                source_file=str(row["source_file"]),
                environment=str(row["environment"]),
                label=row["label"],
                ordinal=int(row["ordinal"]),
                statement_start=int(row["statement_start"]),
                statement_end=int(row["statement_end"]),
                statement_byte_start=int(row["statement_byte_start"]),
                statement_byte_end=int(row["statement_byte_end"]),
                proof_start=row["proof_start"],
                proof_end=row["proof_end"],
                proof_byte_start=row["proof_byte_start"],
                proof_byte_end=row["proof_byte_end"],
                statement_hash=str(row["statement_hash"]),
                proof_hash=str(row["proof_hash"]),
                normalized_statement_hash=str(row["normalized_statement_hash"]),
                statement_text=str(row["statement_text"]),
                proof_text=str(row["proof_text"]),
                references=tuple(json.loads(row["references_json"])),
            )
            for row in store.claim_versions(snapshot)
        )
        edges = tuple(
            ManuscriptEdge(
                str(row["src"]),
                str(row["dst"]),
                str(row["edge_kind"]),
                str(row["provenance"]),
                bool(row["approved"]),
            )
            for row in store.manuscript_edges()
        )
    return objects, edges


def cmd_manuscript_graph(args) -> int:
    from .incremental.graph import graph_to_dot, manuscript_graph_export
    from .incremental.io import atomic_write_json, atomic_write_text
    from .incremental.session import IncrementalSession

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        objects, edges = _incremental_objects_and_edges(session)
        if args.format == "dot":
            rendered = graph_to_dot(objects, edges)
            if args.output:
                atomic_write_text(Path(args.output).expanduser().resolve(), rendered)
            else:
                print(rendered, end="")
        else:
            payload = manuscript_graph_export(objects, edges)
            if args.output:
                atomic_write_json(Path(args.output).expanduser().resolve(), payload)
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_manuscript_questions(args) -> int:
    from .incremental.locking import project_lock
    from .incremental.models import ClaimState
    from .incremental.session import IncrementalSession
    from .incremental.store import StateStore

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        with project_lock(
            session.project, exclusive=bool(args.resolve or args.dismiss)
        ):
            with StateStore(session.database_path) as store:
                question_id = args.resolve or args.dismiss
                if question_id:
                    if not args.reason:
                        raise ValueError(
                            "--reason is required when resolving or dismissing a question"
                        )
                    row = store.connection.execute(
                        "SELECT * FROM clarifications WHERE question_id = ?",
                        (question_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"Unknown clarification: {question_id}")
                    status = "RESOLVED" if args.resolve else "DISMISSED"
                    if not store.resolve_question(
                        question_id,
                        run_id=None,
                        resolution=args.reason,
                        status=status,
                    ):
                        raise ValueError(f"Clarification is not open: {question_id}")
                    store.connection.execute(
                        "UPDATE claims SET status = ? WHERE claim_id = ?",
                        (str(ClaimState.INVALIDATED), row["claim_id"]),
                    )
                    session._write_status_files(store=store)
                    session._commit_host_changes(
                        f"{status.title()} clarification {question_id}"
                    )
                questions = [dict(row) for row in store.open_questions()]
        if args.json:
            print(json.dumps(questions, indent=2, sort_keys=True))
        elif not questions:
            print("No open clarification requests.")
        else:
            for row in questions:
                print(f"{row['question_id']} {row['claim_id']} [{row['category']}]")
                print(f"  {row['problem']}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_manuscript_diff(args) -> int:
    from .incremental.session import IncrementalSession
    from .incremental.store import StateStore

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        with StateStore(session.database_path) as store:
            latest = store.latest_run()
            if latest is None:
                print("No runs have been recorded.")
                return 0
            path = session.runs / f"{int(latest['run_id']):06d}" / "source-diff.patch"
            print(path.read_text(encoding="utf-8") if path.is_file() else "", end="")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_manuscript_invalidate(args) -> int:
    from .incremental.graph import affected_claims
    from .incremental.locking import project_lock
    from .incremental.models import ClaimState
    from .incremental.session import IncrementalSession
    from .incremental.store import StateStore

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        with project_lock(session.project, exclusive=True):
            with StateStore(session.database_path) as store:
                objects, edges = _incremental_objects_and_edges(session)
                all_ids = {item.claim_id for item in objects}
                unknown = sorted(set(args.claim) - all_ids)
                if unknown:
                    raise ValueError("Unknown claims: " + ", ".join(unknown))
                selected = (
                    affected_claims(args.claim, claim_ids=all_ids, edges=edges)
                    if args.include_dependents
                    else set(args.claim)
                )
                for claim_id in selected:
                    store.connection.execute(
                        "UPDATE claims SET status = ? WHERE claim_id = ?",
                        (str(ClaimState.INVALIDATED), claim_id),
                    )
                session._write_status_files(store=store)
                session._commit_host_changes(
                    "Manually invalidate verification certificates"
                )
        print("invalidated: " + ", ".join(sorted(selected)))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_manuscript_audit(args) -> int:
    from .incremental.models import LeanDeclaration
    from .incremental.reports import dependency_audit
    from .incremental.session import IncrementalSession
    from .incremental.store import StateStore

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        _objects, edges = _incremental_objects_and_edges(session)
        with StateStore(session.database_path) as store:
            declarations = tuple(
                LeanDeclaration(
                    name=str(row["name"]),
                    kind=str(row["kind"]),
                    type_hash=str(row["type_hash"]),
                    value_hash=row["value_hash"],
                    direct_dependencies=store.lean_dependencies(str(row["name"])),
                    axioms=tuple(json.loads(row["axioms_json"])),
                )
                for row in store.connection.execute(
                    "SELECT * FROM lean_declarations ORDER BY name"
                )
            )
            payload = dependency_audit(store, edges=edges, declarations=declarations)
        print(json.dumps(payload, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_manuscript_correspondence(args) -> int:
    from .incremental.locking import project_lock
    from .incremental.models import ClaimState
    from .incremental.session import IncrementalSession
    from .incremental.store import StateStore

    session = IncrementalSession(Path(args.project))
    try:
        session._load_config()
        mutating = bool(args.approve or args.reject)
        with project_lock(session.project, exclusive=mutating):
            with StateStore(session.database_path) as store:
                claim_id = args.approve or args.reject
                if claim_id:
                    row = store.connection.execute(
                        "SELECT * FROM correspondence WHERE claim_id = ?",
                        (claim_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"No proposed correspondence for {claim_id}")
                    if args.approve:
                        store.connection.execute(
                            """
                            UPDATE correspondence SET approved = 1, status = 'approved_pending'
                            WHERE claim_id = ?
                            """,
                            (claim_id,),
                        )
                        store.connection.execute(
                            "UPDATE claims SET status = ? WHERE claim_id = ?",
                            (str(ClaimState.STATEMENT_APPROVED), claim_id),
                        )
                        message = f"Approved correspondence for {claim_id}"
                    else:
                        if not args.reason:
                            raise ValueError(
                                "--reason is required when rejecting a correspondence"
                            )
                        store.connection.execute(
                            """
                            UPDATE correspondence SET approved = 0, status = 'rejected'
                            WHERE claim_id = ?
                            """,
                            (claim_id,),
                        )
                        store.connection.execute(
                            "UPDATE claims SET status = ? WHERE claim_id = ?",
                            (str(ClaimState.FAILED_FORMALIZATION), claim_id),
                        )
                        message = (
                            f"Rejected correspondence for {claim_id}: {args.reason}"
                        )
                    session._write_status_files(store=store)
                    session._commit_host_changes(message)
                    print(message)
                rows = [dict(row) for row in store.correspondence_rows()]
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        elif not claim_id:
            if not rows:
                print("No manuscript/Lean correspondences have been proposed.")
            for row in rows:
                approval = "approved" if row["approved"] else "review required"
                print(
                    f"{row['claim_id']} -> {row['lean_declaration']} "
                    f"[{row['status']}; {approval}]"
                )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    return 0


def cmd_tui(args) -> int:
    """Launch the replaceable Textual interface over the workflow contract."""
    from .tui import run_tui
    from .workflow.service import ProofAssistantWorkflow

    service = ProofAssistantWorkflow(
        cache_home=args.cache_home,
        codex=args.codex,
    )
    return int(run_tui(service))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="proof-assistant")
    p.add_argument(
        "--version",
        action="version",
        version=f"Proof Assistant {__version__}",
    )
    p.add_argument("--codex", default="codex", help="Codex executable")
    p.add_argument(
        "--cache-home",
        default=None,
        help=(
            "Package cache root (default: ~/.cache/repoprover-codex; must be "
            "inside the user home, local, and outside Dropbox)"
        ),
    )
    sub = p.add_subparsers(dest="command", required=False)

    tui = sub.add_parser("tui", help="Launch the interactive Proof Assistant")
    tui.set_defaults(func=cmd_tui)

    d = sub.add_parser("doctor", help="Check Codex app-server connectivity")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("models", help="List models/efforts advertised by Codex")
    m.set_defaults(func=cmd_models)

    cc = sub.add_parser(
        "compiler-check", help="Compile and run a native program for Lean/Lake"
    )
    cc.set_defaults(func=cmd_compiler_check)

    cache = sub.add_parser(
        "cache", help="Inspect and initialize validated package cache storage"
    )
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_path = cache_sub.add_parser("path", help="Print the validated cache root")
    cache_path.set_defaults(func=cmd_cache_path)
    cache_init = cache_sub.add_parser(
        "init", help="Create cache directories and record a working compiler"
    )
    cache_init.add_argument(
        "--max-gb",
        type=float,
        default=None,
        help="Managed-cache admission limit in GiB (default: 16)",
    )
    cache_init.add_argument(
        "--min-free-gb",
        type=float,
        default=None,
        help="Minimum filesystem free-space reserve in GiB (default: 25)",
    )
    cache_init.set_defaults(func=cmd_cache_init)
    cache_doctor = cache_sub.add_parser(
        "doctor", help="Validate cache location, layout, and compiler"
    )
    cache_doctor.set_defaults(func=cmd_cache_doctor)
    cache_attach = cache_sub.add_parser(
        "attach", help="Move a project's .lake tree into managed cache storage"
    )
    cache_attach.add_argument("--project", required=True)
    cache_attach.set_defaults(func=cmd_cache_attach)
    cache_status = cache_sub.add_parser(
        "status", help="Show cache usage, limits, and filesystem headroom"
    )
    cache_status.set_defaults(func=cmd_cache_status)
    cache_gc = cache_sub.add_parser(
        "gc", help="Evict inactive coarse cache units to enforce limits"
    )
    cache_gc.add_argument(
        "--gc-timeout",
        type=float,
        default=900.0,
        help="Maximum cache-GC time in seconds (default: 900)",
    )
    cache_gc.set_defaults(func=cmd_cache_gc)
    cache_prepare = cache_sub.add_parser(
        "prepare",
        help="Attach, share dependencies, and build a Lean project without Codex",
    )
    cache_prepare.add_argument("--project", required=True)
    cache_prepare.add_argument("--lean-cc", default=None)
    cache_prepare.add_argument("--setup-timeout", type=float, default=1800.0)
    cache_prepare.add_argument("--gc-timeout", type=float, default=900.0)
    cache_prepare.set_defaults(func=cmd_cache_prepare)

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
    rp.add_argument("--gc-timeout", type=float, default=900.0)
    rp.set_defaults(func=cmd_repoprover_prove)

    manuscript_group = sub.add_parser(
        "manuscript",
        help="Persistent incremental manuscript-verification projects",
    )
    manuscript_sub = manuscript_group.add_subparsers(
        dest="manuscript_command", required=True
    )

    manuscript_init = manuscript_sub.add_parser(
        "init", help="Create a persistent verification project and index its manuscript"
    )
    manuscript_init.add_argument("--manuscript", required=True)
    manuscript_init.add_argument("--project", required=True)
    manuscript_init.set_defaults(func=cmd_manuscript_init)

    manuscript_verify = manuscript_sub.add_parser(
        "verify", help="Snapshot, incrementally verify, and preserve certified results"
    )
    manuscript_verify.add_argument("--project", required=True)
    manuscript_verify.add_argument("--model", required=True)
    manuscript_verify.add_argument("--effort", default="high")
    manuscript_verify.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Independent Codex proof batches to run concurrently (1-2; default: 1)",
    )
    manuscript_verify.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Maximum ready claims assigned to one proof batch (default: 8)",
    )
    manuscript_verify.add_argument("--lean-pool-size", type=int, default=1)
    manuscript_verify.add_argument("--lean-memory-limit-gb", type=int, default=None)
    manuscript_verify.add_argument("--setup-timeout", type=float, default=1800.0)
    manuscript_verify.add_argument("--request-timeout", type=float, default=120.0)
    manuscript_verify.add_argument("--turn-timeout", type=float, default=3600.0)
    manuscript_verify.add_argument("--gc-timeout", type=float, default=900.0)
    manuscript_verify.set_defaults(func=cmd_manuscript_verify)

    manuscript_status = manuscript_sub.add_parser(
        "status", help="Show the current snapshot, claim states, and blockers"
    )
    manuscript_status.add_argument("--project", required=True)
    manuscript_status.add_argument("--json", action="store_true")
    manuscript_status.set_defaults(func=cmd_manuscript_status)

    manuscript_graph = manuscript_sub.add_parser(
        "graph", help="Export the deterministic manuscript dependency graph"
    )
    manuscript_graph.add_argument("--project", required=True)
    manuscript_graph.add_argument("--format", choices=("json", "dot"), default="json")
    manuscript_graph.add_argument("--output", default=None)
    manuscript_graph.set_defaults(func=cmd_manuscript_graph)

    manuscript_questions = manuscript_sub.add_parser(
        "questions", help="List, explicitly resolve, or dismiss clarification requests"
    )
    manuscript_questions.add_argument("--project", required=True)
    question_action = manuscript_questions.add_mutually_exclusive_group()
    question_action.add_argument("--resolve", metavar="QUESTION_ID")
    question_action.add_argument("--dismiss", metavar="QUESTION_ID")
    manuscript_questions.add_argument("--reason", default=None)
    manuscript_questions.add_argument("--json", action="store_true")
    manuscript_questions.set_defaults(func=cmd_manuscript_questions)

    manuscript_diff = manuscript_sub.add_parser(
        "diff", help="Show the latest deterministic manuscript snapshot diff"
    )
    manuscript_diff.add_argument("--project", required=True)
    manuscript_diff.set_defaults(func=cmd_manuscript_diff)

    manuscript_invalidate = manuscript_sub.add_parser(
        "invalidate", help="Explicitly invalidate claims without deleting Lean proofs"
    )
    manuscript_invalidate.add_argument("--project", required=True)
    manuscript_invalidate.add_argument("--claim", action="append", required=True)
    manuscript_invalidate.add_argument("--include-dependents", action="store_true")
    manuscript_invalidate.set_defaults(func=cmd_manuscript_invalidate)

    manuscript_audit = manuscript_sub.add_parser(
        "audit", help="Compare persisted manuscript and Lean proof dependencies"
    )
    manuscript_audit.add_argument("--project", required=True)
    manuscript_audit.set_defaults(func=cmd_manuscript_audit)

    manuscript_correspondence = manuscript_sub.add_parser(
        "correspondence",
        help="Review manuscript-to-Lean statement correspondence proposals",
    )
    manuscript_correspondence.add_argument("--project", required=True)
    correspondence_action = manuscript_correspondence.add_mutually_exclusive_group()
    correspondence_action.add_argument("--approve", metavar="CLAIM_ID")
    correspondence_action.add_argument("--reject", metavar="CLAIM_ID")
    manuscript_correspondence.add_argument("--reason", default=None)
    manuscript_correspondence.add_argument("--json", action="store_true")
    manuscript_correspondence.set_defaults(func=cmd_manuscript_correspondence)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.func = cmd_tui
    try:
        return int(args.func(args))
    finally:
        _release_active_resources()


if __name__ == "__main__":
    raise SystemExit(main())
