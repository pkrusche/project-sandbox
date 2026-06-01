"""Shared Jinja environment for rendering the bundled templates.

Constructing the Environment/loader once avoids rebuilding it on every render
call and gives all renderers a single place to resolve templates and partials.
"""

from jinja2 import Environment, PackageLoader

_ENV = Environment(loader=PackageLoader("project_sandbox", "templates"))


def get_template(name: str):
    return _ENV.get_template(name)
