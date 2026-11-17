"""Terminal styling helpers that degrade cleanly on non-color terminals.

Click strips ANSI sequences when stdout is not a TTY, and also honors
``NO_COLOR`` / ``FORCE_COLOR`` conventions.
"""

from __future__ import annotations

import collections.abc as cabc
from typing import Any

import click
from click.formatting import HelpFormatter

_HELP_OPTION_NAMES = ["-h", "--help", "-help"]


def bold(text: str) -> str:
    return click.style(text, bold=True)


def success(text: str) -> str:
    return click.style(text, fg="green")


def error(text: str) -> str:
    return click.style(text, fg="red")


def warning(text: str) -> str:
    return click.style(text, fg="yellow")


def info(text: str) -> str:
    return click.style(text, fg="cyan")


def muted(text: str) -> str:
    return click.style(text, fg="bright_black")


def path(value: object) -> str:
    return info(str(value))


def heading(text: str) -> str:
    return click.style(text, fg="yellow", bold=True)


def option_name(text: str) -> str:
    return click.style(text, fg="cyan", bold=True)


def command_name(text: str) -> str:
    return click.style(text, fg="green", bold=True)


class ColorHelpFormatter(HelpFormatter):
    """HelpFormatter that colors Usage, section headings, options, and commands."""

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        if prefix is None:
            prefix = f"{heading('Usage:')} "
        styled_prog = bold(prog)
        if args:
            # Dim metavars like OPTIONS / COMMAND and bracketed tokens.
            styled_args = " ".join(
                muted(part) if part.isupper() or part.startswith("[") else part
                for part in args.split()
            )
            super().write_usage(styled_prog, styled_args, prefix=prefix)
        else:
            super().write_usage(styled_prog, "", prefix=prefix)

    def write_heading(self, heading_text: str) -> None:
        super().write_heading(heading(heading_text.rstrip(":")))

    def write_dl(
        self,
        rows: cabc.Iterable[tuple[str, str]],
        col_max: int = 30,
        col_spacing: int = 2,
    ) -> None:
        styled: list[tuple[str, str]] = []
        for name, desc in rows:
            # Command names are bare words; options start with "-".
            styled_name = option_name(name) if name.lstrip().startswith("-") else command_name(name)
            styled.append((styled_name, desc))
        super().write_dl(styled, col_max=col_max, col_spacing=col_spacing)


class ColorContext(click.Context):
    """Context that builds help with :class:`ColorHelpFormatter`."""

    formatter_class = ColorHelpFormatter


class ColorCommand(click.Command):
    """Click command that uses :class:`ColorHelpFormatter` for ``--help``."""

    context_class = ColorContext

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ctx_settings = {"help_option_names": list(_HELP_OPTION_NAMES)}
        user_settings = kwargs.pop("context_settings", None) or {}
        ctx_settings.update(user_settings)
        kwargs["context_settings"] = ctx_settings
        super().__init__(*args, **kwargs)


class ColorGroup(click.Group):
    """Click group with colored help; subcommands inherit the same formatter."""

    context_class = ColorContext
    command_class = ColorCommand

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ctx_settings = {"help_option_names": list(_HELP_OPTION_NAMES)}
        user_settings = kwargs.pop("context_settings", None) or {}
        ctx_settings.update(user_settings)
        kwargs["context_settings"] = ctx_settings
        super().__init__(*args, **kwargs)
