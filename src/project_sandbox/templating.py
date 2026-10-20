"""Shared Jinja environment for rendering the bundled templates.

Constructing the Environment/loader once avoids rebuilding it on every render
call and gives all renderers a single place to resolve templates and partials.
"""

import shlex

from jinja2 import Environment, PackageLoader

from .build_network import docker_env

_ENV = Environment(loader=PackageLoader("project_sandbox", "templates"))
# Defense in depth: let templates shell-quote interpolated values so they cannot
# break out of the surrounding shell context even if validation is bypassed.
_ENV.filters["shlex_quote"] = shlex.quote
_ENV.filters["docker_env"] = docker_env


def get_template(name: str):
    return _ENV.get_template(name)
