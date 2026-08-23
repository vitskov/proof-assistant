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
            user_prompt='Call echo with text "repoprover-codex-smoke".',
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
                "Cache is not initialized; run `repoprover-codex cache init`"
            )
        config = layout.load_config()
        if config is None:
            raise CacheLocationError(
                "Cache compiler configuration is missing; run "
                "`repoprover-codex cache init`"
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
    return 0


def cmd_cache_gc(args) -> int:
    try:
        layout = _cache_layout(args)
        layout.create()
        policy = cache_policy(layout.load_config())
        result = garbage_collect_cache(layout, policy, strict=False)
    except CacheLocationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"before: {_format_gib(result.before.managed_bytes)}")
    print(f"after: {_format_gib(result.after.managed_bytes)}")
    print(f"filesystem free: {_format_gib(result.after.free_bytes)}")
    print(f"removed entries: {len(result.removed)}")
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
    try:
        cache_session = managed_project_session(
            project,
            cache_layout,
            cache_policy(cache_layout.load_config()),
            attach=False,
            reserve_gb=WARM_PROJECT_RESERVE_GB,
            lease_timeout=args.request_timeout,
        )
        cache_session.__enter__()
        _hold_cleanup(lambda session=cache_session: session.__exit__(None, None, None))
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repoprover-codex")
    p.add_argument("--codex", default="codex", help="Codex executable")
    p.add_argument(
        "--cache-home",
        default=None,
        help=(
            "Package cache root (default: ~/.cache/repoprover-codex; must be "
            "inside the user home, local, and outside Dropbox)"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

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
        help="Hard managed-cache limit in GiB (default: 16)",
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
        "gc", help="Evict inactive least-recently-used entries to enforce limits"
    )
    cache_gc.set_defaults(func=cmd_cache_gc)
    cache_prepare = cache_sub.add_parser(
        "prepare",
        help="Attach, share dependencies, and build a Lean project without Codex",
    )
    cache_prepare.add_argument("--project", required=True)
    cache_prepare.add_argument("--lean-cc", default=None)
    cache_prepare.add_argument("--setup-timeout", type=float, default=1800.0)
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
    rp.set_defaults(func=cmd_repoprover_prove)

    manuscript = sub.add_parser(
        "manuscript-run",
        help="Verify a file-specified task against a LaTeX manuscript snapshot",
    )
    manuscript.add_argument(
        "--manuscript",
        required=True,
        help="Folder containing the LaTeX manuscript source",
    )
    manuscript.add_argument(
        "--task-file",
        required=True,
        help="UTF-8 file containing the authoritative free-form task",
    )
    manuscript.add_argument(
        "--output",
        required=True,
        help="New or empty output folder (the Lean workspace must be outside Dropbox)",
    )
    manuscript.add_argument("--model", required=True)
    manuscript.add_argument("--effort", default="high")
    manuscript.add_argument("--lean-pool-size", type=int, default=1)
    manuscript.add_argument(
        "--lean-memory-limit-gb",
        type=int,
        default=None,
        help="Per-REPL address-space limit (default: 0 on macOS, 24 on Linux)",
    )
    manuscript.add_argument(
        "--lean-cc",
        default=None,
        help="Native compiler for Lake (auto-detected when omitted)",
    )
    manuscript.add_argument(
        "--setup-timeout",
        type=float,
        default=1800.0,
        help="Timeout per dependency/bootstrap/final-build command",
    )
    manuscript.add_argument("--request-timeout", type=float, default=120.0)
    manuscript.add_argument("--turn-timeout", type=float, default=3600.0)
    manuscript.set_defaults(func=cmd_manuscript_run)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    finally:
        _release_active_resources()


if __name__ == "__main__":
    raise SystemExit(main())
