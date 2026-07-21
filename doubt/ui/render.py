"""Concise, decision-oriented terminal rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..core.result import InstallResult, PackageCheckResult, PackageEditResult
from ..core.version import VERSION


class PackageView(Protocol):
    @property
    def source(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def apps(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class PresentationContext:
    selected: tuple[str, ...]
    app_lists: tuple[PackageView, ...] = ()
    dependency_lists: tuple[PackageView, ...] = ()
    planning: bool = False
    details: bool = False
    installed: bool = False
    activation_required: int = 0


def selected_sections(_selected: Sequence[str]) -> tuple[str, ...]:
    return ("dependencies", "applications", "setup", "verification")


def product_heading() -> str:
    return f"Doubt {VERSION}"


def render_plan(results: Sequence[InstallResult], context: PresentationContext) -> str:
    failure = _first(results, "fail")
    if failure is not None:
        return _failure("Preflight blocked", failure.name, "Resolve the reported conflict, then run doubt again.")

    counts = _required_counts(results, context.activation_required)
    if sum(counts.values()) == 0:
        return f"{product_heading()}\n\nNo changes required."
    width = max(len(label) for label in counts)
    lines = [product_heading(), "", "Checking workstation...", ""]
    lines.extend(f"{label.ljust(width)}  {count:>4} required" for label, count in counts.items())
    if context.planning:
        lines.extend(("", "Plan complete. No changes were made."))
    return "\n".join(lines)


def render_verification(
    results: Sequence[InstallResult], *, details: bool = False, include_heading: bool = True
) -> str:
    failures = [item for item in results if item.status == "fail"]
    if not failures:
        prefix = f"{product_heading()}\n\n" if include_heading else ""
        return f"{prefix}Verification passed.\nWorkstation ready."
    remote_only = all("metadata unavailable" in item.name.lower() for item in failures)
    lines = [
        "Verification incomplete" if remote_only else "Verification failed",
        "",
        f"Managed state  failed ({len(failures)})",
    ]
    lines.extend(("", "Failures:"))
    lines.extend(f"  - {item.name}" for item in failures)
    action = "Retry `doubt verify`." if remote_only else "Run `doubt` to reconcile managed state."
    lines.extend(("", f"Action: {action}"))
    return "\n".join(lines)


def render_final(
    results: Sequence[InstallResult],
    context: PresentationContext,
    *,
    operational_error: str | None = None,
) -> str:
    failures = [item for item in results if item.status == "fail"]
    if operational_error is not None:
        return _failure(
            "Workstation setup failed",
            operational_error,
            "Correct the reported problem, then run doubt again.",
        )
    counts = _completion_counts(results, context.activation_required)
    if not failures and counts["Installed"] == 0 and counts["Configured"] == 0:
        return "Verification passed.\nWorkstation ready."
    heading = "Complete" if not failures else "Workstation setup incomplete"
    width = max(len(label) for label in counts)
    lines = [heading, ""]
    lines.extend(f"{label.ljust(width)}  {count:>4}" for label, count in counts.items())
    if failures:
        lines.extend(
            (
                "",
                "Failures:",
                "Action: correct the reported problem, then run doubt again.",
            )
        )
        lines[-1:-1] = [f"  - {item.name}" for item in failures]
    else:
        lines.extend(("", "Workstation ready."))
    return "\n".join(lines)


def render_report(results: Sequence[InstallResult], context: PresentationContext) -> str:
    if context.planning:
        return render_plan(results, context)
    if context.selected == ("verify",):
        return render_verification(results, details=context.details)
    return render_final(results, context)


def render_operational_error(message: str, _context: PresentationContext) -> str:
    return _failure(
        "Operation failed",
        message,
        "Correct the reported problem, then run doubt again.",
    )


def _required_counts(results: Sequence[InstallResult], activation: int) -> OrderedDict[str, int]:
    return OrderedDict(
        (
            ("Dependencies", _changed(results, {"pacman deps"})),
            ("Applications", _changed(results, {"pacman apps", "aur", "flatpak"})),
            ("GitHub and SSH", _changed(results, {"auth", "ssh", "github ssh"})),
            ("Git configuration", _changed(results, {"git"})),
            ("Codex profiles", _changed(results, {"codex"})),
            ("Doubt activation", activation),
            ("Launcher setup", 0),
            ("PATH integration", _changed(results, {"path"})),
        )
    )


def _completion_counts(results: Sequence[InstallResult], activation: int) -> OrderedDict[str, int]:
    package_sources = {"pacman deps", "pacman apps", "aur", "flatpak"}
    return OrderedDict(
        (
            ("Installed", sum(item.status == "add" and item.source in package_sources for item in results)),
            ("Configured", activation + _setup_changes(results)),
            ("Unchanged", sum(item.status == "ok" and item.category != "verify" for item in results)),
            ("Failed", sum(item.status == "fail" for item in results)),
        )
    )


def _changed(results: Sequence[InstallResult], sources: set[str]) -> int:
    return sum(item.status in {"add", "warn"} and item.source in sources for item in results)


def _setup_changes(results: Sequence[InstallResult]) -> int:
    package_sources = {"pacman deps", "pacman apps", "aur", "flatpak", "packages", "verify"}
    return sum(item.status in {"add", "warn"} and item.source not in package_sources for item in results)


def _first(results: Sequence[InstallResult], status: str) -> InstallResult | None:
    return next((item for item in results if item.status == status), None)


def _failure(heading: str, reason: str, action: str) -> str:
    return f"{heading}\n\nStatus  failed\n\nReason: {reason}\nAction: {action}"


def render_package_lists(
    package_lists: Sequence[PackageView],
    selected_source: str | None = None,
) -> str:
    sources: OrderedDict[str, OrderedDict[str, list[str]]] = OrderedDict()
    for package_list in package_lists:
        categories = sources.setdefault(package_list.source, OrderedDict())
        categories.setdefault(package_list.category, []).extend(package_list.apps)
    lines: list[str] = [selected_source] if selected_source is not None and not sources else []
    for source, categories in sources.items():
        if lines:
            lines.append("")
        lines.append(source)
        for category, packages in categories.items():
            lines.append(f"  {category}")
            lines.extend(f"    {package}" for package in sorted(packages))
    return "\n".join(lines)


def render_package_check(result: PackageCheckResult) -> str:
    return f"Package declarations are valid: {result.packages} packages across {result.sources} sources."


def render_package_edit(result: PackageEditResult, *, installed: bool = False) -> str:
    if not result.changed:
        verb = "already declared" if result.action == "add" else "not declared"
        return f"{result.package} is {verb}. No packages were changed."
    verb = "Added" if result.action == "add" else "Removed"
    consequence = "No package was installed." if result.action == "add" else "No package was uninstalled."
    return f"{verb} {result.package}. {consequence} Run `{invocation_name(installed)} plan` to review the result."


def invocation_name(installed: bool) -> str:
    return "doubt" if installed else "./install"
