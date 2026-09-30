"""Where the vkit plugin lives, in an installed wheel and in a checkout.

The plugin is not optional packaging. It is the only way a host runs vkit's
hooks, skills and MCP server, so a build that cannot find it is a build that
cannot do its job. That makes "where is it" a fact with one answer per install,
and this module is the one place that answers it.

Two shapes, one function. `resolve_plugin_root()` returns the directory that
holds both `.claude-plugin/marketplace.json` and `plugin/`, because the host
installs from a marketplace whose own `source` is `./plugin`. Returning the
parent keeps that relationship intact instead of splitting it into two lookups
that could disagree.

    an installed wheel   vkit/_plugin_root/   (force-included, see pyproject.toml)
    a source checkout    <repo>/               (plugin/ beside src/, unchanged)

The packaged form is tried first. A contributor running `pip install -e` has
both present, and the installed one is whose bytes were actually installed.

Every resource the plugin needs is reached through this one root, so there is no
second search path to drift out of agreement with the first.

The refusal type is this module's own rather than `console.plan.Refused`, because
`vkit.console.__init__` imports `operations` and `operations` imports this:
importing the console type from here would close that loop. A caller that has
one translates at its own boundary, where its own refusal type lives.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path

PLUGIN_DIR = Path("plugin")
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = Path(".claude-plugin") / "marketplace.json"

# The force-include target directory in pyproject.toml, named as a path under
# the installed `vkit` package rather than as a module: the files are data with
# no __init__.py, and importlib only returns a real path for a join below one.
_PACKAGED_ROOT_DIR = "_plugin_root"

# A checkout has the repository root two levels above src/vkit/.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


class PluginResourcesMissing(Exception):
    """This install carries no plugin package.

    Unreachable from a correctly built wheel, so it means the build is broken
    rather than the request. It names where it looked, because an operator
    seeing it can act on the paths but not on the type.
    """

    def __init__(self, looked_in: list[Path]):
        super().__init__(
            "the vkit plugin package is not installed; looked in "
            f"{', '.join(str(path) for path in looked_in)}. "
            "This build ships the plugin, so its packaging is broken; "
            "reinstall the package or run from a checkout."
        )


@lru_cache(maxsize=1)
def resolve_plugin_root() -> Path:
    """The directory holding `.claude-plugin/` and `plugin/`, or raise.

    Resolved once per process. The answer is fixed for the life of the install:
    the plugin is not relocated underneath a running console, and re-searching
    per call would only make a moved directory look like a different truth.
    """
    for candidate in (_packaged_root(), _CHECKOUT_ROOT):
        if (candidate / PLUGIN_MANIFEST).is_file():
            return candidate
    raise PluginResourcesMissing([_packaged_root(), _CHECKOUT_ROOT])


def plugin_dir() -> Path:
    """The plugin package the host validates and installs (`plugin/`)."""
    return resolve_plugin_root() / PLUGIN_DIR


def _packaged_root() -> Path:
    """The force-included directory, or a path that cannot exist.

    Anchored on the `vkit` package and joined, rather than addressed as
    `vkit._plugin_root` in its own right. The force-included directory has no
    `__init__.py`, so it is not an importable module; naming it directly makes
    importlib return a `MultiplexedPath`, whose `str()` is a repr rather than a
    filesystem path, and whose `as_file()` copy is a temporary extraction deleted
    when the context closes. Neither can be handed to the host, which copies the
    marketplace out of it long afterwards. Joining one level down from a real
    package returns a real path to the installed bytes.

    A package installed somewhere without files on disk yields a path that
    cannot exist, so an unusual install is reported through the same refusal as
    an unusual layout instead of opening a second route through this function.
    """
    try:
        anchored = resources.files("vkit").joinpath(_PACKAGED_ROOT_DIR)
        if isinstance(anchored, Path):
            return anchored
    except (ImportError, TypeError, ValueError):
        pass
    return _CHECKOUT_ROOT / "<no packaged plugin>"
