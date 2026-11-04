"""Which suggested commands may be shown to an operator.

Commands are never executed by this system; a human copies them. They are still checked,
because a model can be wrong or manipulated (a log line saying "run rm -rf /" is
attacker-controlled input), and an operator under pressure may paste what they are given.

The policy is an allowlist of read-only diagnostics, not a blocklist of dangerous ones:
anything it does not recognise is rejected. Shell syntax (pipes, redirection, command
chaining, substitution, variables) is rejected outright, so a command is exactly one
program with literal arguments.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence

MAX_LENGTH = 300
_SHELL_SYNTAX = re.compile(r"[;&|<>`$\\\n\r]")
_ELEVATION = frozenset({"sudo", "su", "doas", "pkexec", "runuser"})

Check = Callable[[Sequence[str]], str | None]


def _any_args(_args: Sequence[str]) -> str | None:
    return None


def _no_args(args: Sequence[str]) -> str | None:
    return "takes no arguments here" if args else None


def _without(*flags: str, reason: str) -> Check:
    def check(args: Sequence[str]) -> str | None:
        for arg in args:
            if arg in flags or any(
                arg.startswith(f"{flag}=") for flag in flags if flag.startswith("--")
            ):
                return f"{arg} {reason}"
        return None

    return check


def _requires(flag: str, reason: str) -> Check:
    def check(args: Sequence[str]) -> str | None:
        return None if any(arg == flag or arg.startswith(flag) for arg in args) else reason

    return check


def _subcommands(allowed: Mapping[str, Check], valued: frozenset[str] = frozenset()) -> Check:
    """The first argument that is not an option (or an option's value) must be allowed."""

    def check(args: Sequence[str]) -> str | None:
        index = 0
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in valued else 1
        if index >= len(args):
            return "needs a read-only subcommand"
        sub = args[index]
        if sub not in allowed:
            return f"'{sub}' is not a read-only subcommand"
        return allowed[sub](args[index + 1 :])

    return check


_FOLLOW = _without("-f", "--follow", "-F", reason="never ends")

_DOCKER_READ = {
    "ps": _any_args,
    "logs": _FOLLOW,
    "inspect": _any_args,
    "stats": _requires("--no-stream", "docker stats needs --no-stream"),
    "top": _any_args,
    "port": _any_args,
    "diff": _any_args,
    "images": _any_args,
    "info": _any_args,
    "version": _any_args,
    "events": _requires("--until", "docker events needs --until, or it never ends"),
}
_DOCKER_COMPOSE_READ = {
    "ps": _any_args,
    "logs": _FOLLOW,
    "top": _any_args,
    "config": _any_args,
    "images": _any_args,
    "port": _any_args,
    "ls": _any_args,
    "version": _any_args,
}
_LISTING = {"ls": _any_args, "inspect": _any_args}

# Options that take a value, so the value is not mistaken for the subcommand.
_COMPOSE_VALUED = frozenset(
    {
        "-p",
        "--project-name",
        "-f",
        "--file",
        "--project-directory",
        "--profile",
        "--env-file",
        "--ansi",
    }
)
_DOCKER_VALUED = frozenset({"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"})

_DOCKER = _subcommands(
    {
        **_DOCKER_READ,
        "compose": _subcommands(_DOCKER_COMPOSE_READ, _COMPOSE_VALUED),
        "container": _subcommands({**_LISTING, "logs": _FOLLOW, "top": _any_args}),
        "image": _subcommands(_LISTING),
        "network": _subcommands(_LISTING),
        "volume": _subcommands(_LISTING),
        "system": _subcommands({"df": _any_args, "info": _any_args}),
    },
    _DOCKER_VALUED,
)

_SYSTEMCTL = _subcommands(
    dict.fromkeys(
        (
            "status",
            "show",
            "cat",
            "is-active",
            "is-enabled",
            "is-failed",
            "list-units",
            "list-timers",
            "list-unit-files",
        ),
        _any_args,
    )
)

_JOURNALCTL = _without(
    "-f",
    "--follow",
    "--vacuum-size",
    "--vacuum-time",
    "--vacuum-files",
    "--rotate",
    "--flush",
    "--sync",
    "--relinquish-var",
    "--setup-keys",
    reason="is not a read-only journalctl option",
)

_KUBECTL = _subcommands(
    dict.fromkeys(
        (
            "get",
            "describe",
            "logs",
            "top",
            "events",
            "explain",
            "version",
            "cluster-info",
            "api-resources",
        ),
        _any_args,
    )
)

_IP_CHANGES = frozenset(
    {"add", "del", "delete", "set", "flush", "change", "replace", "append", "exec"}
)


def _ip(args: Sequence[str]) -> str | None:
    changing = next((arg for arg in args if arg in _IP_CHANGES), None)
    return f"'ip ... {changing}' changes the network" if changing else None


_CURL_FLAGS = frozenset(
    {
        "-s",
        "-S",
        "-sS",
        "-Ss",
        "-f",
        "-fsS",
        "-sSf",
        "-I",
        "--head",
        "-i",
        "-v",
        "-L",
        "--silent",
        "--show-error",
        "--fail",
    }
)
_CURL_VALUED = frozenset({"--max-time", "-m", "--connect-timeout", "-w", "--write-out"})


def _curl(args: Sequence[str]) -> str | None:
    """GET or HEAD a URL and look at the answer; nothing that sends data or writes files."""
    urls = 0
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _CURL_FLAGS:
            index += 1
        elif arg in _CURL_VALUED or (
            arg in ("-o", "--output") and index + 1 < len(args) and args[index + 1] == "/dev/null"
        ):
            index += 2
        elif arg.startswith(("http://", "https://")):
            urls += 1
            index += 1
        else:
            return f"curl option {arg!r} is not allowed (it could send data or write files)"
    return None if urls == 1 else "curl needs exactly one http(s) URL"


_DMESG = _without(
    "-c",
    "-C",
    "--clear",
    "--read-clear",
    "-D",
    "-E",
    "--console-off",
    "--console-on",
    "-n",
    "--console-level",
    reason="changes the kernel log",
)
_READABLE_PREFIXES = (
    "/proc/",
    "/sys/",
    "/var/log/",
    "/etc/os-release",
    "/etc/resolv.conf",
    "/etc/hosts",
)


def _cat(args: Sequence[str]) -> str | None:
    files = [arg for arg in args if not arg.startswith("-")]
    if not files:
        return "cat needs a file"
    outside = next((f for f in files if not f.startswith(_READABLE_PREFIXES)), None)
    return f"cat of {outside} is outside the diagnostic paths" if outside else None


def _top(args: Sequence[str]) -> str | None:
    return (
        None
        if "-b" in args and "-n" in args
        else "top needs -b -n 1 (batch mode), or it never ends"
    )


def _ping(args: Sequence[str]) -> str | None:
    return None if "-c" in args else "ping needs -c, or it never ends"


PROGRAMS: dict[str, Check] = {
    "docker": _DOCKER,
    "systemctl": _SYSTEMCTL,
    "journalctl": _JOURNALCTL,
    "kubectl": _KUBECTL,
    "ip": _ip,
    "curl": _curl,
    "dmesg": _DMESG,
    "cat": _cat,
    "top": _top,
    "ping": _ping,
    "tail": _FOLLOW,
    **dict.fromkeys(
        (
            "df",
            "du",
            "free",
            "uptime",
            "uname",
            "hostname",
            "date",
            "who",
            "w",
            "id",
            "last",
            "ps",
            "pgrep",
            "vmstat",
            "iostat",
            "mpstat",
            "pidstat",
            "sar",
            "lsblk",
            "lscpu",
            "nproc",
            "findmnt",
            "lsof",
            "ss",
            "netstat",
            "dig",
            "nslookup",
            "host",
            "getent",
            "traceroute",
            "tracepath",
            "stat",
            "ls",
            "wc",
            "head",
            "grep",
        ),
        _any_args,
    ),
    # Plain listing only: with arguments, mount mounts.
    "mount": _no_args,
}


def _tokens(command: str) -> tuple[list[str], str | None]:
    """The command's words, or why it is not a single plain command."""
    if len(command) > MAX_LENGTH:
        return [], f"longer than {MAX_LENGTH} characters"
    if _SHELL_SYNTAX.search(command):
        return [], "uses shell syntax (pipes, redirection, chaining, substitution or variables)"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [], "cannot be parsed as a single command"
    return tokens, None if tokens else "is empty"


def _program_reason(program: str, args: Sequence[str]) -> str | None:
    if program in _ELEVATION:
        return f"{program} runs with elevated privileges"
    check = PROGRAMS.get(program)
    if check is None:
        return f"{program} is not an allowed read-only diagnostic"
    if any(arg in ("-exec", "--exec", "-delete") for arg in args):
        return "runs or deletes something"
    reason = check(args)
    return f"{program}: {reason}" if reason else None


def rejection_reason(command: str) -> str | None:
    """None if the command may be shown; otherwise why not."""
    tokens, problem = _tokens(command)
    if problem is not None:
        return problem
    return _program_reason(tokens[0].rsplit("/", 1)[-1], tokens[1:])
