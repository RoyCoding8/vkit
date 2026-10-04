"""The two registered logic rules, and the checker whose verdict they rest on.

Checkpoint 1 classifies comments and proves a file is otherwise unchanged.
This module owns the other half: two narrow syntactic transformations, and the
compiled-representation check that decides whether either may be written without
a human reading the patch.

## What is registered, and what is not

Two rule ids exist and no others. `LOGIC-EMPTY-ELSE-PASS` removes a trailing
`else: pass`. `LOGIC-REDUNDANT-PASS` removes a `pass` from a body that holds at
least one other statement.

That is the whole registry. There is no dead-branch deletion, no algebraic
identity, no import removal, no duplicate-call collapsing, no boolean
simplification, and no unused-function removal. Each is a different
transformation needing its own equivalence argument, and the plan declines to
generalize. A rule that cannot pass the checker is not registered in a weakened
form; it is reported as not applicable automatically.

A `pass` that is the ONLY statement of its body is never a candidate, and that
is not a heuristic. `try`, `except`, `finally`, `else` and a bare `if` each
require a non-empty body, so the lone `pass` is load-bearing syntax rather than
a leftover. The requirement `len(body) >= 2` is where that fact is kept.

## The measurement that decided this file's shape

The plan records a probe: on CPython 3.13.14 an empty `else: pass` example had
equal executable fields and an interior redundant `pass` example changed
bytecode. Re-measured here, per plan, on the same interpreter. It holds -- and
the reasons are specific enough to write down, because they are why the rules
are as narrow as they are:

* A trailing `else: pass` compiles to nothing extra when the `if` or `for` it
  belongs to is the LAST statement of its parent body. Equal at optimization
  levels 0, 1 and 2. In mid-body position the `NOP` is emitted and survives,
  because the following statement needs a jump target to point at.
* `while ... else: pass` differs at every level even in trailing position. CPython
  places the loop's implicit fall-through `RETURN_CONST None` AFTER the `else`
  body, so the branch is not empty in the compiled form. `for ... else: pass` is
  equal, because `END_FOR` already occupies that position. So the rule admits
  `If` and `For` and refuses `While`, on measurement rather than on taste.
* A trailing `pass` in a module, function, class or block is equal at all three
  levels. A pass in a MIDDLE position is not, for the jump-target reason above.
  Every middle-position case measured changed bytecode: after an assignment,
  inside an `if` body, and inside a `try` body, where it additionally rewrote
  `co_exceptiontable`.
* Removing a `pass` from in front of a string changes `__doc__`. The compiled
  fields changed in every case measured too, so the compiled check catches these
  on its own -- but for the incidental reason that a `NOP` vanished. The
  docstring check below is stated explicitly rather than assumed, so the
  guarantee does not depend on that coincidence holding for some future
  compiler.

Nothing here normalizes a difference away. A patch that fails the checker is a
suggestion, and the refusal names the field that differed.

## The checker

`verify_compiled_equality(before, after)` compiles both texts at optimization
levels 0, 1 and 2 and compares the whole nested code-object tree.

**Constants are compared by exact representation.** A checker using `==` on
`co_consts` certifies `x = 0.0` and `x = -0.0` as interchangeable, because
`0.0 == -0.0` is `True`. Measured: those two sources compile to byte-identical
`co_code` and to `co_consts` tuples that compare equal, so every other field
agrees and only the constant's exact form separates them -- while `repr`, `str`
and `math.copysign` all tell them apart at runtime. Floats are keyed by
hexadecimal float representation, complex numbers by both parts, tuples and
frozensets recursively, and the TYPE is part of every key, so `1`, `True` and
`1.0` are three constants rather than one.

**Only source-location metadata is excluded.** Every field is compared except
the ones in `EXCLUDED_LOCATION_FIELDS`, each of which is listed with the reason
it is metadata. `co_code`, `co_consts`, `co_names`, `co_varnames`, `co_freevars`,
`co_cellvars`, `co_argcount`, `co_posonlyargcount`, `co_kwonlyargcount`,
`co_nlocals`, `co_stacksize`, `co_flags` and `co_exceptiontable` are compared
in full, at every level, in the module and in every nested code object.

**An unknown code-object field is a refusal.** The checker compares the fields a
real code object carries on this interpreter against the ones it knows how to
compare. A field it has never heard of means its coverage claim is inaccurate,
and the honest response to an inaccurate coverage claim is to decline.

**An unsupported interpreter is a refusal.** The claim is scoped to the compiler
that produced the code objects. It is not extrapolated to another version.

**The scope, stated.** Compiled equality is not program equivalence. Python code
can read `__doc__`, walk `co_consts`, inspect `co_firstlineno`, or trace lines,
and those observations can differ while every field compared here is equal. The
plan requires exactly this scope to be stated at enrollment, and it is.
"""
from __future__ import annotations

import ast
import hashlib
import platform
import sys
import types
from dataclasses import dataclass
from typing import Any, Sequence

from .comments import _check_path, _read_source


REQUIRED_OPTIMIZE_LEVELS: tuple[int, ...] = (0, 1, 2)

TESTED_CPYTHON_VERSIONS: frozenset[str] = frozenset({"3.13.14"})

CHECKER_ID = "vkit.cleanup.logic.compiled-equality/1"

COMPARED_FIELDS: tuple[str, ...] = (
    "co_code",
    "co_consts",
    "co_names",
    "co_varnames",
    "co_freevars",
    "co_cellvars",
    "co_argcount",
    "co_posonlyargcount",
    "co_kwonlyargcount",
    "co_nlocals",
    "co_stacksize",
    "co_flags",
    "co_exceptiontable",
    "co_name",
    "co_qualname",
)

EXCLUDED_LOCATION_FIELDS: dict[str, str] = {
    "co_filename": "the file the code was compiled from, not what it does",
    "co_firstlineno": "the definition's first line in the source",
    "co_linetable": "the line table, which is source positions by another name",
    "co_lines": "a lazy view of co_linetable, the same metadata",
    "co_positions": "a lazy column view of the line table, the same metadata",
    "co_lnotab": (
        "the pre-3.10 encoding of the line table, kept as a deprecated alias of "
        "co_linetable; the census below found it still present on 3.13"
    ),
}

PRIVATE_CODE_ATTRS: frozenset[str] = frozenset(
    {"_co_code_adaptive", "_varname_from_oparg", "replace"}
)

CONST_TYPES: tuple[type, ...] = (
    type(None), type(Ellipsis), type(NotImplemented),
    bool, int, float, complex, str, bytes, bytearray,
    tuple, frozenset, types.CodeType,
)




def _exact_constant(value: Any) -> tuple:
    """An exact-representation key for one constant.

    A float is keyed by its hexadecimal representation rather than its value,
    because `==` on floats is deliberately not identity: `0.0 == -0.0` is True
    while the two are distinguishable by `repr`, `str` and `math.copysign`, and
    `nan != nan` would report one unchanged constant as changed on every run.

    The type is the first element of every key, so `1`, `True` and `1.0` are
    three different constants rather than one.
    """
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, complex):
        return ("complex", value.real.hex(), value.imag.hex())
    if isinstance(value, tuple):
        return ("tuple", tuple(_exact_constant(item) for item in value))
    if isinstance(value, frozenset):
        return ("frozenset", frozenset(_exact_constant(item) for item in value))
    if isinstance(value, types.CodeType):
        return ("code", _exact_code(value))
    return (type(value).__name__, value)


def _exact_code(code: types.CodeType) -> tuple:
    """The exact-representation key for one code object and everything in it."""
    fields = tuple(
        (name, _exact_constant(value) if name == "co_consts" else value)
        for name in COMPARED_FIELDS
        for value in (getattr(code, name, None),)
    )
    nested = tuple(
        _exact_code(child) for child in code.co_consts if isinstance(child, types.CodeType)
    )
    return fields + (("nested", nested),)


def _constant_keys(constants: Sequence[Any]) -> tuple:
    """A `co_consts` tuple reduced to exact-representation keys, recursively."""
    return tuple(_exact_constant(value) for value in constants)


def _walk(code: types.CodeType) -> list[tuple[str, types.CodeType]]:
    """Every code object in the tree, in compilation order, each with its path.

    A list rather than a mapping keyed by name: two definitions can share a name
    in different scopes, and a mapping would collapse them into one entry and
    compare a function against itself.
    """
    found = [("<module>", code)]
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            found.extend((f"{path}/{child.co_name}", obj) for path, obj in _walk(child))
    return found


def _constant_types(value: Any) -> set[str]:
    """Every type reachable inside one constant, for the unknown-type census."""
    found = {type(value).__name__}
    if isinstance(value, (tuple, frozenset)):
        for item in value:
            found |= _constant_types(item)
    elif isinstance(value, types.CodeType):
        for _path, obj in _walk(value):
            for constant in obj.co_consts:
                found |= _constant_types(constant)
    return found




@dataclass(frozen=True)
class CompiledEqual:
    """The two texts compile to the same executable representation, at every level."""

    before_digest: str
    after_digest: str
    python_version: str
    implementation: str
    optimize_levels: tuple[int, ...]
    checked_fields: tuple[str, ...]
    excluded_fields: tuple[str, ...]
    code_objects: int

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "PASS",
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "pythonVersion": self.python_version,
            "implementation": self.implementation,
            "checker": CHECKER_ID,
            "optimizeLevels": list(self.optimize_levels),
            "checkedFields": list(self.checked_fields),
            "excludedFields": list(self.excluded_fields),
            "codeObjects": self.code_objects,
        }


@dataclass(frozen=True)
class CompiledDiffers:
    """The compiled forms differ, or the text will not compile at all.

    The patch becomes a suggestion. It is never a failure to apply and never a
    partial write.
    """

    reason: str
    detail: str
    python_version: str
    optimize_level: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "FAIL",
            "reason": self.reason,
            "detail": self.detail,
            "pythonVersion": self.python_version,
            "optimizeLevel": self.optimize_level,
            "checker": CHECKER_ID,
        }


@dataclass(frozen=True)
class CompiledUnsupported:
    """The claim cannot be made on this host, and it is declined rather than weakened."""

    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"result": "UNSUPPORTED", "reason": self.reason, "detail": self.detail}


CompiledCheck = CompiledEqual | CompiledDiffers | CompiledUnsupported


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _census() -> CompiledUnsupported | None:
    """Whether this host supports the comparison at all, and why not when it does not.

    Three refusals, in the order they can apply. The field census is the one
    that matters over time: a `CodeType` carrying an attribute this file has
    never heard of means `COMPARED_FIELDS` is out of date, and a checker that
    compared what it knew and reported PASS would be making a coverage claim it
    cannot support.
    """
    implementation = platform.python_implementation()
    if implementation != "CPython":
        return CompiledUnsupported(
            "unsupported_implementation",
            f"the compiled-equality check is scoped to CPython, because it compares "
            f"CPython code objects; this host runs {implementation}",
        )
    version = platform.python_version()
    if version not in TESTED_CPYTHON_VERSIONS:
        return CompiledUnsupported(
            "unsupported_python_version",
            f"CPython {version} has not been measured by {CHECKER_ID}. Compiled "
            f"equality is scoped to the compiler that produced the code objects, so "
            f"the claim is refused rather than extrapolated; the measured versions "
            f"are {sorted(TESTED_CPYTHON_VERSIONS)}",
        )
    observed = {
        name
        for name in dir(types.CodeType)
        if not name.startswith("__") and not name.endswith("__")
    } - set(EXCLUDED_LOCATION_FIELDS) - PRIVATE_CODE_ATTRS
    unknown = sorted(observed - set(COMPARED_FIELDS))
    if unknown:
        return CompiledUnsupported(
            "unknown_code_field",
            f"this interpreter's code objects carry {unknown}, which {CHECKER_ID} "
            "does not compare. Its coverage claim is therefore not accurate for this "
            "build, and a comparison it cannot make in full is one it declines to "
            "report as a pass",
        )
    if not sys.implementation.cache_tag.startswith("cpython"):
        return CompiledUnsupported(
            "unsupported_implementation",
            f"this interpreter reports cache tag {sys.implementation.cache_tag!r}, "
            "so co_code payloads are not CPython wordcode this checker can compare",
        )
    return None


def _compile_each_level(text: str, filename: str) -> list[types.CodeType]:
    return [compile(text, filename, "exec", dont_inherit=True, optimize=level)
            for level in REQUIRED_OPTIMIZE_LEVELS]


def _first_divergence(before: types.CodeType, after: types.CodeType) -> str | None:
    """The first executable field that differs, addressed the way a reader can follow it."""
    left, right = _walk(before), _walk(after)
    if len(left) != len(right):
        return (
            f"the compiled forms nest differently: {len(left)} code objects against "
            f"{len(right)}"
        )
    for (path, code_a), (_, code_b) in zip(left, right):
        for field in COMPARED_FIELDS:
            value_a, value_b = getattr(code_a, field, None), getattr(code_b, field, None)
            if field == "co_consts":
                if _constant_keys(value_a) != _constant_keys(value_b):
                    return (
                        f"{path}.{field} differs by exact representation "
                        f"({list(_constant_keys(value_a))} against "
                        f"{list(_constant_keys(value_b))})"
                    )
                continue
            if value_a != value_b:
                return f"{path}.{field} differs"
    return None


def _unknown_constants(code: types.CodeType) -> str | None:
    """The first constant whose type has no exact comparison defined here."""
    known = {kind.__name__ for kind in CONST_TYPES}
    for path, obj in _walk(code):
        for constant in obj.co_consts:
            extra = _constant_types(constant) - known
            if extra:
                return (
                    f"{path} holds a constant of type {sorted(extra)}, which has no "
                    "exact comparison defined; comparing it with == would repeat the "
                    "defect this checker exists to avoid"
                )
    return None


def verify_compiled_equality(
    before: bytes, after: bytes, *, filename: str = "sample.py"
) -> CompiledCheck:
    """Whether two byte strings compile to the same executable representation.

    Compares the entire module and every nested code object, at optimization
    levels 0, 1 and 2, field by field, with constants keyed by exact
    representation.

    An unrecognised constant type is a difference rather than a pass, for the
    same reason a float is keyed by bits rather than by value: this checker does
    not compare what it cannot compare exactly.
    """
    census = _census()
    if census is not None:
        return census
    try:
        before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
    except UnicodeDecodeError as exc:
        return CompiledUnsupported("undecodable_source", str(exc))

    try:
        before_codes = _compile_each_level(before_text, f"<before:{filename}>")
        after_codes = _compile_each_level(after_text, f"<after:{filename}>")
    except (SyntaxError, ValueError) as exc:
        return CompiledDiffers("does_not_compile", str(exc), platform.python_version())

    for level, code_a, code_b in zip(REQUIRED_OPTIMIZE_LEVELS, before_codes, after_codes):
        unknown = _unknown_constants(code_a) or _unknown_constants(code_b)
        if unknown is not None:
            return CompiledDiffers(
                "unknown_constant_type", unknown, platform.python_version(), level
            )
        divergence = _first_divergence(code_a, code_b)
        if divergence is not None:
            return CompiledDiffers(
                "executable_fields_differ", divergence, platform.python_version(), level
            )

    return CompiledEqual(
        before_digest=_digest(before),
        after_digest=_digest(after),
        python_version=platform.python_version(),
        implementation=platform.python_implementation(),
        optimize_levels=REQUIRED_OPTIMIZE_LEVELS,
        checked_fields=COMPARED_FIELDS,
        excluded_fields=tuple(sorted(EXCLUDED_LOCATION_FIELDS)),
        code_objects=len(_walk(before_codes[0])),
    )



_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def static_docstrings(source: str) -> dict[str, str | None]:
    """Every docstring CPython would install, keyed by the definition's path.

    Read from the AST rather than by executing the module. `compile` treats a
    leading string expression as a docstring for exactly these node types and no
    others, and nothing else about that rule depends on running the code, so
    running the file to find out would import it.

    A definition with no docstring maps to None rather than being absent, because
    "this function has none" and "this function is gone" are different facts and
    a comparison that could not tell them apart would pass a patch that deleted a
    definition.
    """
    found: dict[str, str | None] = {}
    _collect_docstrings(ast.parse(source), "", found)
    return found


def _collect_docstrings(node: ast.AST, path: str, found: dict[str, str | None]) -> None:
    """Record `node`'s own docstring, then recurse into its children.

    The node is recorded before the loop because a `Module`'s docstring lives on
    the module node itself, and iterating only children would leave the module's
    `__doc__` uncompared -- which is exactly the promotion `pass` before a
    module docstring performs.
    """
    if isinstance(node, _DOCSTRING_OWNERS):
        name = "<module>" if isinstance(node, ast.Module) else node.name
        here = "<module>" if isinstance(node, ast.Module) else (
            f"{path}/{name}" if path else f"/{name}"
        )
        found[here] = _leading_string(node.body)
        path = here
    for child in ast.iter_child_nodes(node):
        _collect_docstrings(child, path, found)


def _leading_string(body: Sequence[ast.stmt]) -> str | None:
    if not body:
        return None
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value.value
    return None


def verify_docstrings(before: bytes, after: bytes) -> CompiledCheck:
    """Whether both texts install the same docstring on every definition.

    A separate claim with its own verdict, not an assumption folded into the
    compiled comparison. Removing a `pass` from in front of a string changes
    `__doc__`, and the compiled check also catches those patches today -- but it
    catches them because a `NOP` vanished, not because anything was compared.
    Stating the comparison means the guarantee does not rest on that coincidence.
    """
    try:
        before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
        before_docs, after_docs = static_docstrings(before_text), static_docstrings(after_text)
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        return CompiledDiffers("does_not_parse", str(exc), platform.python_version())

    for path in sorted(set(before_docs) | set(after_docs)):
        if before_docs.get(path) != after_docs.get(path):
            return CompiledDiffers(
                "docstring_changed",
                f"{path}: __doc__ moved from {before_docs.get(path)!r} to "
                f"{after_docs.get(path)!r}. A pass removed in front of a string "
                "promotes that string to the definition's docstring, which is an "
                "observable change even where the bytecode happens to match",
                platform.python_version(),
            )
    return CompiledEqual(
        before_digest=_digest(before),
        after_digest=_digest(after),
        python_version=platform.python_version(),
        implementation=platform.python_implementation(),
        optimize_levels=(),
        checked_fields=("__doc__",),
        excluded_fields=(),
        code_objects=len(before_docs),
    )



EMPTY_ELSE_PASS = "LOGIC-EMPTY-ELSE-PASS"
REDUNDANT_PASS = "LOGIC-REDUNDANT-PASS"

REGISTERED_RULES: tuple[str, ...] = (EMPTY_ELSE_PASS, REDUNDANT_PASS)

EMPTY_ELSE_OWNERS: tuple[type, ...] = (ast.If, ast.For)


@dataclass(frozen=True)
class LogicSite:
    """One statement a rule would remove, addressed by the lines it occupies."""

    rule_id: str
    line: int
    start_line: int
    end_line: int
    description: str

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "line": self.line,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "description": self.description,
        }


@dataclass(frozen=True)
class LogicProposal:
    """One authorized transformation. Nothing has been written."""

    relative_path: str
    proposal_id: str
    rule_id: str
    before_bytes: bytes
    after_bytes: bytes
    sites: tuple[LogicSite, ...]
    compiled: CompiledEqual
    docstrings: CompiledEqual
    optimize_levels: tuple[int, ...]
    python_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "PROPOSAL",
            "path": self.relative_path,
            "proposalId": self.proposal_id,
            "rule": self.rule_id,
            "beforeDigest": self.compiled.before_digest,
            "afterDigest": self.compiled.after_digest,
            "sites": [site.to_json() for site in self.sites],
            "compiledEquality": self.compiled.to_json(),
            "docstringEquality": self.docstrings.to_json(),
            "optimizeLevels": list(self.optimize_levels),
            "pythonVersion": self.python_version,
            "checker": CHECKER_ID,
        }


@dataclass(frozen=True)
class LogicRefusal:
    """No authorized transformation. `reason` is required; that is the variant."""

    relative_path: str
    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "REFUSED",
            "path": self.relative_path,
            "reason": self.reason,
            "detail": self.detail,
        }


LogicPreview = LogicProposal | LogicRefusal


class LogicReason:
    """The refusal vocabulary, as constants rather than a second enum.

    The values are strings because they cross into the policy module's
    structured result, and one vocabulary spelled in one place is what stops the
    two from drifting.
    """

    PATH = "path"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed_source"
    UNKNOWN_RULE = "unknown_rule"
    NO_CANDIDATE = "no_candidate"
    CANDIDATE_UNVERIFIED = "candidate_unverified"


def _is_pass(node: ast.AST) -> bool:
    return isinstance(node, ast.Pass)


def _split_lines(source: str) -> list[str]:
    """Physical lines with their terminators, so joining the survivors rebuilds the file."""
    return source.splitlines(keepends=True)


def _else_start(lines: Sequence[str], pass_line: int) -> int:
    """The first line of an `else: pass`, which may share a line with the pass.

    `pass` on a line of its own puts `else:` on the line above; `else: pass` puts
    both on one. The previous line counts only when it starts an `else` and does
    not itself carry a `pass`, which is what distinguishes the two spellings
    without a token pass.
    """
    if pass_line < 2:
        return pass_line
    previous = lines[pass_line - 2].strip()
    if previous.startswith("else") and "pass" not in previous:
        return pass_line - 1
    return pass_line


def _collect(tree: ast.Module, lines: Sequence[str]) -> dict[str, list[LogicSite]]:
    """Every candidate site, grouped by rule, in source order."""
    by_rule: dict[str, list[LogicSite]] = {}

    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list) or len(body) < 2:
                continue
            for statement in body:
                if not _is_pass(statement):
                    continue
                line = statement.lineno
                by_rule.setdefault(REDUNDANT_PASS, []).append(
                    LogicSite(
                        rule_id=REDUNDANT_PASS,
                        line=line,
                        start_line=line,
                        end_line=statement.end_lineno or line,
                        description=(
                            f"pass alongside {len(body) - 1} other statement(s) in a "
                            f"{type(node).__name__} body"
                        ),
                    )
                )

        if isinstance(node, EMPTY_ELSE_OWNERS):
            orelse = node.orelse
            if len(orelse) == 1 and _is_pass(orelse[0]):
                line = orelse[0].lineno
                by_rule.setdefault(EMPTY_ELSE_PASS, []).append(
                    LogicSite(
                        rule_id=EMPTY_ELSE_PASS,
                        line=line,
                        start_line=_else_start(lines, line),
                        end_line=orelse[0].end_lineno or line,
                        description=(
                            f"empty else: pass on a {type(node).__name__.lower()}"
                        ),
                    )
                )

    for sites in by_rule.values():
        sites.sort(key=lambda site: (site.start_line, site.end_line))
    return by_rule


def _apply_removals(lines: Sequence[str], sites: Sequence[LogicSite]) -> bytes | None:
    """The source with those line spans cut out, or None if they overlap.

    Works on text lines rather than by rewriting the AST, so the patch is the
    registered syntactic transformation and nothing else. Overlapping spans mean
    two candidates share a line, which is a shape this module does not know how
    to describe, and it declines rather than guessing which cut wins.
    """
    ordered = sorted(sites, key=lambda site: (site.start_line, site.end_line))
    for left, right in zip(ordered, ordered[1:]):
        if right.start_line <= left.end_line:
            return None
    dropped = {
        line
        for site in ordered
        for line in range(site.start_line, site.end_line + 1)
    }
    return "".join(
        text for number, text in enumerate(lines, start=1) if number not in dropped
    ).encode("utf-8")


def _proposal_id(
    relative_path: str, compiled: CompiledEqual, rule: str, sites: Sequence[LogicSite]
) -> str:
    """A deterministic name derived from content, with no clock and no counter.

    Previewing the same file twice yields the same id, so an apply can bind to
    it without carrying any state: repeating a request converges by arithmetic
    rather than by remembering what happened last time.
    """
    running = hashlib.sha256()
    spans = ",".join(f"{site.start_line}-{site.end_line}" for site in sites)
    for field in (
        relative_path, rule, compiled.before_digest, compiled.after_digest,
        compiled.python_version, spans,
    ):
        running.update(field.encode("utf-8"))
        running.update(b"\x00")
    return running.hexdigest()


def preview_logic_cleanup(
    project: Any,
    relative_path: str,
    *,
    rule_ids: Sequence[str] = REGISTERED_RULES,
) -> LogicPreview:
    """Propose a registered logic transformation for one Python file.

    Nothing is written. Each rule is tried in the order given, and a rule whose
    checker reports a difference produces no proposal at all: the plan's
    instruction is that differing executable fields leave the patch as a
    suggestion, and this returns the refusal that names which field differed.

    A rule removes ALL of its candidates in one patch rather than picking the
    easiest one. A patch that removed some passes and left others would be
    partial surgery whose remaining half still has to be reviewed anyway.
    """
    refusal = _check_path(project, relative_path)
    if refusal is not None:
        return LogicRefusal(relative_path, refusal.reason.value, refusal.detail)

    unknown = [rule for rule in rule_ids if rule not in REGISTERED_RULES]
    if unknown:
        return LogicRefusal(
            relative_path,
            LogicReason.UNKNOWN_RULE,
            f"rule id(s) {unknown} are not registered; the registered rules are "
            f"{list(REGISTERED_RULES)}",
        )

    try:
        data = (project.root / relative_path).read_bytes()
    except OSError as exc:
        return LogicRefusal(relative_path, LogicReason.UNREADABLE, str(exc))

    try:
        source = _read_source(data)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return LogicRefusal(relative_path, LogicReason.MALFORMED, str(exc))
    try:
        tree = ast.parse(source.text)
    except (SyntaxError, ValueError) as exc:
        return LogicRefusal(relative_path, LogicReason.MALFORMED, str(exc))

    lines = _split_lines(source.text)
    by_rule = _collect(tree, lines)
    refusals: list[str] = []

    for rule in rule_ids:
        sites = by_rule.get(rule, [])
        if not sites:
            continue
        after_bytes = _apply_removals(lines, sites)
        if after_bytes is None:
            refusals.append(f"{rule}: candidate line spans overlap")
            continue
        compiled = verify_compiled_equality(data, after_bytes, filename=relative_path)
        if not isinstance(compiled, CompiledEqual):
            refusals.append(f"{rule}: {compiled.reason}: {compiled.detail}")
            continue
        docs = verify_docstrings(data, after_bytes)
        if not isinstance(docs, CompiledEqual):
            refusals.append(f"{rule}: {docs.reason}: {docs.detail}")
            continue
        return LogicProposal(
            relative_path=relative_path,
            proposal_id=_proposal_id(relative_path, compiled, rule, sites),
            rule_id=rule,
            before_bytes=data,
            after_bytes=after_bytes,
            sites=tuple(sites),
            compiled=compiled,
            docstrings=docs,
            optimize_levels=REQUIRED_OPTIMIZE_LEVELS,
            python_version=compiled.python_version,
        )

    if refusals:
        return LogicRefusal(
            relative_path,
            LogicReason.CANDIDATE_UNVERIFIED,
            "a candidate was found but its preservation check refused it: "
            + "; ".join(refusals),
        )
    return LogicRefusal(
        relative_path,
        LogicReason.NO_CANDIDATE,
        "the file has no candidate site for the registered rules",
    )
