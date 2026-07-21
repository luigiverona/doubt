from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .core.failure import OperationalError
from .core.plan import Mode, PackageRequest, Request, resolve
from .core.result import InstallResult
from .core.task import TASK_ORDER
from .packages import edit as package_edit
from .packages import resolve as package_preflight
from .packages.lists import PackageList, active_state, load_lists
from .system import activation
from .system.lock import MutationLock
from .system.run import CommandRunner
from .system.work import WorkRoot
from .tasks import apps, codex, deps, ssh, verify
from .tasks import git as git_task
from .tasks import path as path_task
from .tasks.github import task as github
from .ui import prompt
from .ui.render import (
    PresentationContext,
    render_final,
    render_package_check,
    render_package_edit,
    render_package_lists,
    render_plan,
    render_verification,
)

ProjectLists = tuple[list[PackageList], list[PackageList]]


def load_project_lists(apps_dir: Path, deps_dir: Path) -> ProjectLists:
    app_lists = load_lists(apps_dir)
    dependency_lists = load_lists(deps_dir)
    return app_lists, dependency_lists


def execute(
    request: Request,
    runner: CommandRunner | None = None,
    startup_loader: Callable[[Callable[[], ProjectLists]], ProjectLists] | None = None,
    confirm: Callable[[], bool] | None = None,
) -> int:
    try:
        with WorkRoot() as workspace:
            return _execute(
                request,
                workspace,
                runner,
                startup_loader,
                confirm,
            )
    except OperationalError as error:
        prompt.line(f"invalid: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        prompt.line(f"invalid: {error}", file=sys.stderr)
        return 1


def _execute(
    request: Request,
    workspace: WorkRoot,
    runner: CommandRunner | None,
    startup_loader: Callable[[Callable[[], ProjectLists]], ProjectLists] | None,
    confirm: Callable[[], bool] | None,
) -> int:
    selected = ("verify",) if request.mode is Mode.VERIFY else resolve(request.selected, request.excluded)
    environment = workspace.environment()
    active_runner = runner or CommandRunner(
        details=request.details,
        writer=prompt.line,
        environment=environment,
        terminal=True,
    )
    if hasattr(active_runner, "environment"):
        active_runner.environment.update(environment)
    if hasattr(active_runner, "details"):
        active_runner.details = request.details

    def load() -> ProjectLists:
        return load_project_lists(request.apps, request.deps)

    app_lists, dependency_lists = startup_loader(load) if startup_loader is not None else load()
    if hasattr(active_runner, "available_commands"):
        declared_dependencies = {name for package_list in dependency_lists for name in package_list.apps}
        commands = {
            "git": "git",
            "flatpak": "flatpak",
            "github-cli": "gh",
            "openssh": "ssh",
        }
        active_runner.available_commands.update(
            command for package, command in commands.items() if package in declared_dependencies
        )
        if "openssh" in declared_dependencies:
            active_runner.available_commands.add("ssh-keygen")
    context = PresentationContext(
        selected=selected,
        app_lists=tuple(app_lists),
        dependency_lists=tuple(dependency_lists),
        details=request.details,
        installed=request.installed,
    )

    if request.mode is Mode.VERIFY:
        active_runner.dry_run = False
        verify_results = run_installers(
            app_lists, dependency_lists, active_runner, selected, planning=False
        )
        prompt.line(render_verification(verify_results, details=request.details))
        return 1 if has_failed_results(verify_results) else 0

    active_runner.dry_run = True
    activation_plan = activation.inspect()
    plan_results = run_installers(
        app_lists,
        dependency_lists,
        active_runner,
        selected,
        planning=True,
    )
    plan_context = PresentationContext(
        selected=context.selected,
        app_lists=context.app_lists,
        dependency_lists=context.dependency_lists,
        planning=request.mode is Mode.PLAN,
        details=context.details,
        installed=context.installed,
        activation_required=activation_plan.required,
    )
    prompt.line(render_plan(plan_results, plan_context))
    if has_failed_results(plan_results):
        return 1
    if request.mode is Mode.PLAN:
        return 0

    required = activation_plan.required + sum(
        item.status in {"add", "warn"} for item in plan_results if item.category != "verify"
    )
    if required == 0:
        active_runner.dry_run = False
        prompt.line()
        prompt.line("Verifying workstation...")
        verification = verify.run(app_lists, dependency_lists, active_runner)
        if has_failed_results(verification):
            prompt.line()
            prompt.line(render_verification(verification, details=request.details, include_heading=False))
            return 1
        prompt.line(render_verification(verification, details=request.details, include_heading=False))
        return 0

    prompt.line()
    if not (confirm or prompt.confirm)():
        prompt.line("Declined. No changes were made.")
        return 0
    prompt.line()

    results: list[InstallResult] = []
    operational_error: str | None = None
    applied = activation_plan
    state_loaded = False
    active_runner.dry_run = False
    with MutationLock(active_runner.home_directory()):
        try:
            applied = activation.apply(activation_plan)
            state = active_state()
            app_lists, dependency_lists = load_project_lists(state.apps, state.deps)
            state_loaded = True
            run_installers(
                app_lists,
                dependency_lists,
                active_runner,
                selected,
                planning=False,
                results=results,
                progress=_progress,
            )
        except OperationalError as error:
            operational_error = str(error)
            if state_loaded:
                prompt.line()
                prompt.line("Verifying workstation")
                try:
                    results.extend(verify.run(app_lists, dependency_lists, active_runner))
                except OperationalError as verification_error:
                    results.append(
                        InstallResult(str(verification_error), "verify", "verify", "fail")
                    )
                prompt.line("failed")
                prompt.line()

    final_context = PresentationContext(
        selected=selected,
        app_lists=tuple(app_lists),
        dependency_lists=tuple(dependency_lists),
        details=request.details,
        installed=True,
        activation_required=activation_plan.required - applied.required,
    )
    prompt.line(render_final(results, final_context, operational_error=operational_error))
    return 1 if operational_error is not None or has_failed_results(results) else 0


def _progress(label: str, state: str) -> None:
    prompt.line(label if state == "start" else state)
    if state != "start":
        prompt.line()


def execute_package(request: PackageRequest) -> int:
    try:
        state = active_state(require_materialized=True)
        if request.action == "list":
            prompt.line(
                render_package_lists(
                    package_edit.listing(state, request.source, request.category),
                    request.source,
                )
            )
        elif request.action == "check":
            prompt.line(render_package_check(package_edit.check(state)))
        elif request.action == "add":
            if request.source is None or request.category is None or request.package is None:
                raise RuntimeError("incomplete package add request")
            prompt.line(
                render_package_edit(
                    package_edit.add(
                        state,
                        request.source,
                        request.category,
                        request.package,
                        dry_run=request.dry_run,
                    ),
                    installed=state.installed,
                )
            )
        elif request.action == "remove":
            if request.source is None or request.package is None:
                raise RuntimeError("incomplete package remove request")
            prompt.line(
                render_package_edit(
                    package_edit.remove(
                        state,
                        request.source,
                        request.package,
                        dry_run=request.dry_run,
                    ),
                    installed=state.installed,
                )
            )
        else:
            raise RuntimeError(f"unknown package action: {request.action}")
    except (OperationalError, ValueError) as error:
        prompt.line(f"invalid: {error}", file=sys.stderr)
        return 1
    return 0


def run_installers(
    app_lists: Sequence[PackageList],
    dependency_lists: Sequence[PackageList],
    runner: CommandRunner,
    selected_tasks: Sequence[str] = TASK_ORDER,
    *,
    planning: bool | None = None,
    results: list[InstallResult] | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> list[InstallResult]:
    selected = set(selected_tasks)
    planning = runner.dry_run if planning is None else planning

    collected = [] if results is None else results
    if selected.intersection({"deps", "apps", "codex"}):
        conflict_results, safe = package_preflight.preflight(
            app_lists,
            dependency_lists,
            selected_tasks,
            runner,
        )
        collected.extend(conflict_results)
        if not safe:
            return collected

    if "deps" in selected:
        before = len(collected)
        _progress_event(progress, "Installing dependencies", "start")
        collected.extend(deps.run(dependency_lists, runner))
        _progress_event(progress, "Installing dependencies", stage_state(collected[before:]))
    elif "codex" in selected:
        before = len(collected)
        _progress_event(progress, "Installing dependencies", "start")
        collected.extend(deps.run(dependency_lists, runner, category="codex"))
        _progress_event(progress, "Installing dependencies", stage_state(collected[before:]))

    if "apps" in selected:
        before = len(collected)
        _progress_event(progress, "Installing applications", "start")
        collected.extend(apps.run(app_lists, dependency_lists, runner))
        _progress_event(progress, "Installing applications", stage_state(collected[before:]))

    if "github" in selected:
        before = len(collected)
        _progress_event(progress, "Configuring GitHub", "start")
        collected.extend(github.run(runner))
        _progress_event(progress, "Configuring GitHub", stage_state(collected[before:]))

    if "ssh" in selected:
        before = len(collected)
        _progress_event(progress, "Configuring SSH", "start")
        collected.extend(ssh.run(runner))
        _progress_event(progress, "Configuring SSH", stage_state(collected[before:]))

    if "git" in selected:
        before = len(collected)
        _progress_event(progress, "Configuring Git", "start")
        collected.extend(git_task.run(runner, input_fn=prompt.read))
        _progress_event(progress, "Configuring Git", stage_state(collected[before:]))

    if "codex" in selected:
        codex_dependencies = [
            result for result in collected if result.source == "pacman deps" and result.category == "codex"
        ]
        if not any(result.status == "fail" for result in codex_dependencies):
            before = len(collected)
            _progress_event(progress, "Configuring Codex 01", "start")
            _progress_event(progress, "Configuring Codex 02", "start")
            collected.extend(codex.run(runner))
            profile_results = collected[before:]
            for label in ("01", "02"):
                matching = [item for item in profile_results if f" {label}" in f" {item.name}"]
                _progress_event(progress, f"Configuring Codex {label}", stage_state(matching))

    if "path" in selected:
        before = len(collected)
        _progress_event(progress, "Configuring launcher PATH", "start")
        collected.extend(path_task.run(runner.home_directory(), dry_run=runner.dry_run, environment=runner.environment))
        _progress_event(progress, "Configuring launcher PATH", stage_state(collected[before:]))

    if "verify" in selected:
        if planning and len(selected_tasks) > 1:
            collected.append(
                InstallResult(
                    "final verification after planned changes",
                    "verify",
                    "verify",
                    "ok",
                )
            )
        else:
            before = len(collected)
            _progress_event(progress, "Verifying workstation", "start")
            collected.extend(
                verify.run(
                    app_lists,
                    dependency_lists,
                    runner,
                    warn_only=planning,
                )
            )
            _progress_event(progress, "Verifying workstation", stage_state(collected[before:]))

    return collected


def _progress_event(
    progress: Callable[[str, str], None] | None,
    label: str,
    state: str,
) -> None:
    if progress is not None:
        progress(label, state)


def has_failed_results(results: Sequence[InstallResult]) -> bool:
    return any(result.status == "fail" for result in results)


def stage_state(results: Sequence[InstallResult]) -> str:
    if any(item.status == "fail" for item in results):
        return "failed"
    if any(item.status == "warn" for item in results):
        return "warning"
    if any(item.status == "add" for item in results):
        return "done"
    return "unchanged"
