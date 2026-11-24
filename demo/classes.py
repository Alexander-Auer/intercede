"""The class diagram of interCEde, read from the package itself.

Every module under ``intercede`` is imported and every class defined there is
taken, so a class added to the library appears here without a change to the demo.
What the page draws comes from the code alone:

- **Kind:** protocol, Pydantic model, dataclass, enumeration, exception and so on,
  from what the class is.
- **Inheritance:** a base that is also an interCEde class.
- **Implements:** a protocol whose members the class has. The protocols are
  structural, so this is how Python's type checkers see it. Only the most specific
  protocol is drawn: a class that is a ``JobBackend`` is not drawn again as each
  protocol ``JobBackend`` is made of.
- **Holds:** a field, or an attribute ``__init__`` sets, whose annotation names
  another interCEde class.
- **Uses:** a method parameter or return value that names one.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import functools
import importlib
import inspect
import pkgutil
import re
import textwrap
import typing
from pathlib import Path
from types import ModuleType
from typing import Any, get_protocol_members, is_protocol

import intercede

ROOT = Path(intercede.__file__).parents[1]
_IDENTIFIER = re.compile(r"[A-Za-z_][\w.]*")


@functools.cache
def model() -> dict[str, Any]:
    """The diagram's data: areas, classes and relations, ready for the page."""
    modules = _modules()
    found = [
        (cls, module)
        for module in modules
        for name, cls in vars(module).items()
        if inspect.isclass(cls)
        and cls.__module__ == module.__name__
        and cls.__qualname__ == name
        and name.isidentifier()  # not Pydantic's OpOutcome[None], kept in the module by that name
    ]
    ids = {cls: f"c{number}" for number, (cls, _) in enumerate(found)}
    by_name: dict[str, list[type]] = {}
    for cls in ids:
        by_name.setdefault(cls.__name__, []).append(cls)

    def resolve(annotation: Any, module: ModuleType) -> list[type]:
        """The interCEde classes an annotation (a string, as written) names."""
        named = []
        for word in _IDENTIFIER.findall(str(annotation)):
            target: Any = module
            for part in word.split("."):
                target = getattr(target, part, None)
            if target not in ids:
                last = word.rsplit(".", 1)[-1]
                # Only imported under TYPE_CHECKING: the name, if no other class has it.
                candidates = by_name.get(last, [])
                target = candidates[0] if len(candidates) == 1 else None
            if target is not None and target not in named:
                named.append(target)
        return named

    classes = []
    relations: list[dict[str, Any]] = []
    held: dict[tuple[type, type], list[str]] = {}
    used: set[tuple[type, type]] = set()
    for cls, module in found:
        fields = _fields(cls)
        methods = _methods(cls)
        classes.append(
            {
                "id": ids[cls],
                "name": cls.__name__,
                "module": module.__name__,
                "area": _area(module.__name__),
                "kind": _kind(cls),
                "doc": _prose(inspect.getdoc(cls)) if cls.__doc__ else "",
                "where": _where(cls),
                "fields": fields,
                "methods": methods,
            }
        )
        for base in cls.__bases__:
            if base in ids:
                relations.append(
                    {"kind": "inherits", "from": ids[cls], "to": ids[base]}
                )
        for field in fields:
            for target in resolve(field["type"], module):
                if target is not cls:
                    held.setdefault((cls, target), []).append(field["name"])
        for method in methods:
            for annotation in [*method["types"], method["returns"]]:
                for target in resolve(annotation, module):
                    if target is not cls:
                        used.add((cls, target))
    for (cls, target), names in held.items():
        relations.append(
            {
                "kind": "holds",
                "from": ids[cls],
                "to": ids[target],
                "label": ", ".join(names),
            }
        )
    for cls, target in used:
        if (cls, target) not in held:
            relations.append({"kind": "uses", "from": ids[cls], "to": ids[target]})
    relations += [
        {"kind": "implements", "from": ids[cls], "to": ids[protocol]}
        for cls, protocol in _implements(list(ids))
    ]

    areas = sorted({c["area"] for c in classes}, key=lambda a: (a.count("."), a))
    return {
        "package": intercede.__name__,
        "areas": [{"name": area, "doc": _area_doc(area, modules)} for area in areas],
        "classes": classes,
        "relations": relations,
    }


def source(class_id: str) -> dict[str, Any] | None:
    """One class's source, for a class the diagram has; ``None`` for any other id."""
    entry = next((c for c in model()["classes"] if c["id"] == class_id), None)
    if entry is None:
        return None
    module = importlib.import_module(entry["module"])
    cls = getattr(module, entry["name"])
    return {"where": entry["where"], "source": textwrap.dedent(inspect.getsource(cls))}


def _modules() -> list[ModuleType]:
    """Every module of the package; one that cannot be imported here is left out."""
    modules = [intercede]
    for info in pkgutil.walk_packages(intercede.__path__, f"{intercede.__name__}."):
        try:
            modules.append(importlib.import_module(info.name))
        except ImportError:  # an optional dependency this environment lacks
            continue
    return modules


def _area(module: str) -> str:
    """The package a module is in, or the module itself for a top-level one."""
    parts = module.split(".")
    package = importlib.import_module(module)
    if hasattr(package, "__path__"):  # an __init__.py
        return module
    return ".".join(parts[:-1]) if len(parts) > 2 else module  # noqa: PLR2004


def _area_doc(area: str, modules: list[ModuleType]) -> str:
    module = next(m for m in modules if m.__name__ == area)
    doc = inspect.getdoc(module) or ""
    return doc.split("\n\n")[0]


def _prose(doc: str) -> str:
    """A docstring with its paragraphs unwrapped; lists and indented blocks kept as written."""
    paragraphs = []
    for paragraph in doc.split("\n\n"):
        lines = paragraph.splitlines()
        kept = any(line.startswith((" ", "-", "*", ">>>")) for line in lines)
        paragraphs.append(
            paragraph if kept else " ".join(line.strip() for line in lines)
        )
    return "\n\n".join(paragraphs)


def _kind(cls: type) -> str:
    if is_protocol(cls):
        return "protocol"
    if issubclass(cls, BaseException):
        return "exception"
    if issubclass(cls, enum.Enum):
        return "enumeration"
    if typing.is_typeddict(cls):
        return "typed dict"
    if issubclass(cls, tuple) and hasattr(cls, "_fields"):
        return "named tuple"
    if dataclasses.is_dataclass(cls):
        return "dataclass"
    if hasattr(cls, "model_fields"):
        return "model"
    if inspect.isabstract(cls):
        return "abstract"
    return "class"


def _where(cls: type) -> str:
    try:
        path = Path(inspect.getsourcefile(cls) or "")
        line = inspect.getsourcelines(cls)[1]
    except (OSError, TypeError):
        return ""
    return f"{path.relative_to(ROOT.parent) if path.is_relative_to(ROOT.parent) else path}:{line}"


def _own_annotations(cls: type) -> dict[str, Any]:
    return dict(cls.__dict__.get("__annotations__", {}))


def _fields(cls: type) -> list[dict[str, Any]]:
    """Fields as written: members of an enumeration, declared fields, and what ``__init__`` sets."""
    if issubclass(cls, enum.Enum):
        return [
            {"name": member.name, "type": "", "private": False}
            for member in cls  # type: ignore[var-annotated]
        ]
    fields = {
        name: str(annotation)
        for name, annotation in _own_annotations(cls).items()
        if not str(annotation).startswith(("ClassVar", "typing.ClassVar"))
    }
    model_fields = cls.__dict__.get("__pydantic_fields__")
    if isinstance(model_fields, dict):
        for name in model_fields:
            fields.setdefault(name, "")
    for name, value in vars(cls).items():
        if isinstance(value, property):
            returns = (
                inspect.signature(value.fget).return_annotation if value.fget else ""
            )
            fields.setdefault(
                name, "" if returns is inspect.Signature.empty else str(returns)
            )
    if "__init__" in vars(cls) and not is_protocol(cls):
        for name, annotation in _set_by_init(cls).items():
            fields.setdefault(name, annotation)
    return [
        {"name": name, "type": annotation, "private": name.startswith("_")}
        for name, annotation in fields.items()
    ]


def _set_by_init(cls: type) -> dict[str, str]:
    """The attributes ``__init__`` sets on ``self``, with their annotation or the parameter's."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls.__init__)))  # type: ignore[misc]
    except (OSError, TypeError, SyntaxError):
        return {}
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return {}
    parameters = {
        a.arg: ast.unparse(a.annotation)
        for a in [*function.args.args, *function.args.kwonlyargs]
        if a.annotation is not None
    }
    found: dict[str, str] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.AnnAssign):
            targets, annotation = [node.target], ast.unparse(node.annotation)
        elif isinstance(node, ast.Assign):
            value = node.value
            annotation = (
                parameters.get(value.id, "") if isinstance(value, ast.Name) else ""
            )
            targets = node.targets
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                if annotation or target.attr not in found:
                    found[target.attr] = annotation
    return found


def _methods(cls: type) -> list[dict[str, Any]]:
    """The methods the class itself defines; dunders other than ``__init__`` are left out."""
    methods = []
    for name, value in vars(cls).items():
        function = (
            value.__func__ if isinstance(value, classmethod | staticmethod) else value
        )
        if not inspect.isfunction(function):
            continue
        if name.startswith("__") and name != "__init__":
            continue
        if not function.__qualname__.startswith(f"{cls.__qualname__}."):
            continue  # put there from elsewhere, as typing does a protocol's __init__
        signature = inspect.signature(function)
        parameters = [
            p for p in signature.parameters.values() if p.name not in ("self", "cls")
        ]
        returns = signature.return_annotation
        methods.append(
            {
                "name": name,
                "private": name.startswith("_") and name != "__init__",
                "async": inspect.iscoroutinefunction(function),
                "static": isinstance(value, staticmethod | classmethod),
                "parameters": [p.name for p in parameters],
                "types": [
                    str(p.annotation)
                    for p in parameters
                    if p.annotation is not inspect.Parameter.empty
                ],
                "returns": "" if returns is inspect.Signature.empty else str(returns),
                "signature": _signature(name, parameters, returns),
                "doc": _prose(
                    inspect.cleandoc(function.__doc__ or "").split("\n\n")[0]
                ),
            }
        )
    return methods


def _signature(name: str, parameters: list[inspect.Parameter], returns: Any) -> str:
    """A signature as the source writes it: no ``self``, annotations unquoted."""
    written = []
    star = False
    for p in parameters:
        if p.kind is p.VAR_POSITIONAL:
            star = True
            text = f"*{p.name}"
        elif p.kind is p.VAR_KEYWORD:
            text = f"**{p.name}"
        else:
            if p.kind is p.KEYWORD_ONLY and not star:
                written.append("*")
                star = True
            text = p.name
        if p.annotation is not p.empty:
            text += f": {p.annotation}"
        if p.default is not p.empty:
            text += f" = {p.default!r}"
        written.append(text)
    arrow = "" if returns is inspect.Signature.empty else f" -> {returns}"
    return f"{name}({', '.join(written)}){arrow}"


def _implements(classes: list[type]) -> list[tuple[type, type]]:
    """Each class with the most specific protocols it has every member of."""
    protocols = [c for c in classes if is_protocol(c) and get_protocol_members(c)]
    pairs = []
    for cls in classes:
        if is_protocol(cls):
            continue
        names = set(dir(cls)) | {f["name"] for f in _fields(cls)}
        met = [
            p
            for p in protocols
            if all(_has(cls, p, member, names) for member in get_protocol_members(p))
        ]
        pairs += [
            (cls, p)
            for p in met
            if p not in cls.__mro__
            and not any(q is not p and p in q.__mro__ for q in met)
        ]
    return pairs


def _has(cls: type, protocol: type, member: str, names: set[str]) -> bool:
    """Whether the class has the member as the protocol means it.

    A method must take the same parameters and be async alike: a name alone would
    match a class that merely has a method called ``submit`` or ``get``.
    """
    if member not in names:
        return False
    wanted = getattr(protocol, member, None)
    if not inspect.isfunction(wanted) or member.startswith("__"):
        return True
    have = inspect.getattr_static(cls, member, None)
    have = have.__func__ if isinstance(have, classmethod | staticmethod) else have
    if not inspect.isfunction(have):
        return False

    def parameters(function: Any) -> list[str]:
        return [p for p in inspect.signature(function).parameters if p != "self"]

    return parameters(have) == parameters(wanted) and (
        inspect.iscoroutinefunction(have) == inspect.iscoroutinefunction(wanted)
    )


def declared() -> list[tuple[str, str]]:
    """The (class, protocol) pairs the code itself states as ``name: type[P] = C``."""
    pairs = []
    for module in _modules():
        for name, annotation in vars(module).get("__annotations__", {}).items():
            value = getattr(module, name, None)
            match = re.fullmatch(r"type\[(\w+)\]", str(annotation))
            if match and inspect.isclass(value):
                pairs.append((value.__name__, match[1]))
    return pairs
