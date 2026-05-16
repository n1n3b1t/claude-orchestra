"""Entry point for the `orchestra` CLI."""
from __future__ import annotations

import typer

from orchestra import __version__
from orchestra.cli import app


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        typer.echo(f"orchestra {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
