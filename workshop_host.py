"""Runs one tool IO built for itself (workshop/<id>/tool.py) as an MCP server, or checks it.

    python workshop_host.py <folder>           serve its TOOLS over stdio (IO starts it like a plugin)
    python workshop_host.py <folder> --check   print {"ok", "problems", "tools"} as JSON and exit

A workshop tool is plain Python: functions with type-hinted parameters and a docstring, listed in TOOLS. This host does
the MCP part against the installed mcp package, so a tool never depends on how a version of the SDK spells things (mcp
2.x renamed FastMCP to MCPServer; a tool written from memory of 1.x would have broken).
"""
import contextlib
import functools
import importlib.util
import inspect
import json
import sys
import typing
from pathlib import Path

SIMPLE = (str, int, float, bool)


def _ok_type(t) -> bool:
    if t in SIMPLE:
        return True
    origin = typing.get_origin(t)
    if origin in (list, tuple):
        return all(a in SIMPLE for a in typing.get_args(t)) or not typing.get_args(t)
    if origin is typing.Union or str(origin) == "types.UnionType":
        return all(a is type(None) or a in SIMPLE for a in typing.get_args(t))
    return t is dict


def load(folder: Path):
    """(the tool functions, problems with them). Importing runs tool.py's top level, as starting it would."""
    sys.path.insert(0, str(folder))
    if (folder / "_deps").is_dir():  # packages installed for this tool alone; after IO's own, so they never replace mcp's
        sys.path.append(str(folder / "_deps"))
    spec = importlib.util.spec_from_file_location("tool", folder / "tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fns = getattr(mod, "TOOLS", None)
    problems = []
    if not isinstance(fns, (list, tuple)) or not fns:
        return [], ["tool.py needs TOOLS = [function, ...] listing the functions IO may call"]
    for f in fns:
        if not inspect.isfunction(f):
            problems.append(f"TOOLS holds {f!r}, which isn't a plain function")
            continue
        if not (inspect.getdoc(f) or "").strip():
            problems.append(f"{f.__name__} needs a docstring: its first line is what the brain reads to choose it")
        try:
            hints = typing.get_type_hints(f)
        except Exception as e:
            problems.append(f"{f.__name__}'s type hints don't resolve: {e}")
            continue
        for name, p in inspect.signature(f).parameters.items():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                problems.append(f"{f.__name__}(*{name}): name every parameter")
            elif name not in hints:
                problems.append(f"{f.__name__}: parameter {name} needs a type hint (str, int, float, bool or list[str])")
            elif not _ok_type(hints[name]):
                problems.append(f"{f.__name__}: parameter {name} is {hints[name]}; use str, int, float, bool or a list of them")
        if hints.get("return") not in (str, None) and "return" in hints:
            problems.append(f"{f.__name__} should return str (the text the brain reads)")
    return list(fns), problems


def _quiet(f):
    """print() inside a tool would go into the MCP channel on stdout and break it: it goes to stderr instead."""
    @functools.wraps(f)
    def run(*a, **k):
        with contextlib.redirect_stdout(sys.stderr):
            out = f(*a, **k)
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
    return run


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = Path(args[0]).resolve()
    if "--check" in sys.argv:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                fns, problems = load(folder)
        except Exception as e:
            print(json.dumps({"ok": False, "problems": [f"tool.py didn't load: {type(e).__name__}: {e}"], "tools": []}))
            return
        tools = [{"name": f.__name__, "signature": str(inspect.signature(f)),
                  "does": (inspect.getdoc(f) or "").strip().splitlines()[0] if (inspect.getdoc(f) or "").strip() else ""}
                 for f in fns if inspect.isfunction(f)]
        print(json.dumps({"ok": not problems, "problems": problems, "tools": tools}))
        return
    from mcp.server.mcpserver import MCPServer  # mcp 2.x
    with contextlib.redirect_stdout(sys.stderr):
        fns, problems = load(folder)
    if problems:
        print("workshop tool not loaded: " + "; ".join(problems), file=sys.stderr)
        sys.exit(1)
    server = MCPServer(folder.name, log_level="WARNING")  # not a line on IO's console per request
    for f in fns:
        server.add_tool(_quiet(f), name=f.__name__, description=inspect.getdoc(f))
    server.run("stdio")


if __name__ == "__main__":
    main()
