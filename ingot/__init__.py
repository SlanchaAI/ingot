"""Ingot's command surface.

Deliberately empty of imports. `ingot.cli` must stay runnable with nothing installed beyond the
skill loader's own dependency, so anything that reaches for the server, the optimizer, or a model
belongs in the subcommand that needs it, imported inside the function."""
__version__ = "0.2.0"
