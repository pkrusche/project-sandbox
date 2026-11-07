"""Shared Jinja environment for rendering the bundled templates.

Constructing the Environment/loader once avoids rebuilding it on every render
call and gives all renderers a single place to resolve templates and partials.
"""

import shlex

from jinja2 import Environment, PackageLoader

_ENV = Environment(loader=PackageLoader("project_sandbox", "templates"))
# Defense in depth: let templates shell-quote interpolated values so they cannot
# break out of the surrounding shell context even if validation is bypassed.
_ENV.filters["shlex_quote"] = shlex.quote


# Line-oriented configs (squid.conf) need block tags to leave no blank lines.
_TRIMMED_ENV = Environment(
    loader=PackageLoader("project_sandbox", "templates"),
    trim_blocks=True,
    lstrip_blocks=True,
)
_TRIMMED_ENV.filters["shlex_quote"] = shlex.quote


def get_template(name: str, *, trim_blocks: bool = False):
    return (_TRIMMED_ENV if trim_blocks else _ENV).get_template(name)
