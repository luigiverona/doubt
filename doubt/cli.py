"""Small public command-line boundary."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import app
from .core.plan import Mode, PackageRequest, Request
from .core.version import VERSION
from .packages.lists import active_state, runtime_installed
from .system.run import CommandRunner
from .ui import prompt
from .ui.render import invocation_name

TOP_HELP = """usage:
  {program}
  {program} plan
  {program} verify
  {program} pkg list
  {program} pkg add SOURCE CATEGORY PACKAGE
  {program} pkg remove SOURCE PACKAGE
  {program} pkg check
  {program} --version
  {program} --help

commands:
  plan       show required changes without modifying the workstation
  verify     verify managed workstation state without repairing it
  pkg        inspect or edit package declarations; never install or remove packages

option:
  --verbose  show diagnostic commands and detailed failures
  --version  show the installed Doubt version
  --help     show this help

Running {program} without a command plans, confirms once, reconciles, and verifies."""


class CliError(ValueError):
    """Expected command-line validation failure."""


@dataclass(frozen=True)
class Parsed:
    request: Request | None = None
    package: PackageRequest | None = None
    action: str | None = None
    installed: bool = False


def parse_command(argv: Sequence[str]) -> Parsed:
    arguments = list(argv)
    if "-h" in arguments:
        raise CliError("unrecognized argument: -h")

    installed = runtime_installed()
    if arguments in (["--help"], ["--version"]):
        return Parsed(action=arguments[0].removeprefix("--"), installed=installed)

    verbose = False
    if "--verbose" in arguments:
        if arguments.count("--verbose") != 1:
            raise CliError("--verbose may be specified only once")
        arguments.remove("--verbose")
        verbose = True

    unknown_options = [value for value in arguments if value.startswith("-")]
    if unknown_options:
        raise CliError(f"unrecognized argument: {unknown_options[0]}")

    if arguments and arguments[0] == "pkg":
        if verbose:
            raise CliError("--verbose is not accepted by package declaration commands")
        return Parsed(package=_parse_package(arguments[1:]), installed=installed)

    if len(arguments) > 1:
        raise CliError(f"unexpected argument: {arguments[1]}")
    command = arguments[0] if arguments else None
    if command not in {None, "plan", "verify"}:
        raise CliError(f"unknown command: {command}")

    mode = {
        None: Mode.MUTATE,
        "plan": Mode.PLAN,
        "verify": Mode.VERIFY,
    }[command]
    state = active_state()
    return Parsed(
        request=Request(
            mode=mode,
            details=verbose,
            apps=state.apps,
            deps=state.deps,
            installed=state.installed,
        ),
        installed=installed,
    )


def _parse_package(arguments: Sequence[str]) -> PackageRequest:
    if not arguments:
        raise CliError("missing pkg command; run `doubt --help`")
    command = arguments[0]
    values = list(arguments[1:])
    if command not in {"list", "add", "remove", "check"}:
        raise CliError(f"unknown pkg command: {command}")
    expected = {"list": (0, 2), "add": (3, 3), "remove": (2, 2), "check": (0, 0)}[command]
    if not expected[0] <= len(values) <= expected[1]:
        raise CliError(f"invalid arguments for `pkg {command}`; run `doubt --help`")
    if command == "list":
        return PackageRequest(command, values[0] if values else None, values[1] if len(values) == 2 else None)
    if command == "add":
        return PackageRequest(command, values[0], values[1], values[2])
    if command == "remove":
        return PackageRequest(command, values[0], package=values[1])
    return PackageRequest(command)


def help_text(*, installed: bool = False) -> str:
    return TOP_HELP.format(program=invocation_name(installed))


def main(
    argv: Sequence[str] | None = None,
    runner: CommandRunner | None = None,
    startup_loader: Callable[[Callable[[], app.ProjectLists]], app.ProjectLists] | None = None,
    confirm: Callable[[], bool] | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        parsed = parse_command(arguments)
        if parsed.action == "help":
            prompt.line(help_text(installed=parsed.installed))
            return 0
        if parsed.action == "version":
            prompt.line(f"Doubt {VERSION}")
            return 0
        if parsed.package is not None:
            return app.execute_package(parsed.package)
        if parsed.request is None:
            raise RuntimeError("command parser produced no application request")
        return app.execute(parsed.request, runner, startup_loader, confirm)
    except CliError as error:
        prompt.line(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        prompt.line("Cancelled.", file=sys.stderr)
        return 130
