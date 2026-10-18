"""Closed interprocedural C heap model for the existing CBMC safety operation."""
from __future__ import annotations

import re
from typing import Any

from .c_safety import _UnsupportedSource, _scalar_type

PROFILE = "heap-callgraph"
MAX_FUNCTIONS = 16
MAX_ALLOCATION_BYTES = 4_096
MODEL_SCOPE = (
    "Closed acyclic C call graph with scalar entry inputs, initialized locals, single-level pointers, "
    "and fixed-size malloc/free. C11 Linux LP64; allocation can fail. Checks include invalid "
    "dereferences, double/invalid frees and leaked allocations on terminating paths. No external "
    "libraries, threads, globals, structs, dynamic allocation sizes or uninitialized-read guarantee."
)


def validate_and_generate(source: bytes, function: str, parser: Any) -> bytes:
    c_ast, c_generator, c_parser = parser
    try:
        tree = c_parser.CParser().parse(source.decode("utf-8").replace("\r\n", "\n"), filename="candidate.c")
    except (c_parser.ParseError, RecursionError) as exc:
        raise _UnsupportedSource("source is not valid in the closed C heap subset") from exc
    if (not 1 <= len(tree.ext) <= MAX_FUNCTIONS
            or any(not isinstance(item, c_ast.FuncDef) for item in tree.ext)):
        raise _UnsupportedSource("heap source needs one to 16 function definitions and no other declarations")
    names = [item.decl.name for item in tree.ext]
    if len(set(names)) != len(names) or function not in names:
        raise _UnsupportedSource("functions must have distinct names including the requested entry")
    reserved = {"malloc", "free", "main"}
    if any(not _identifier(name) or name in reserved for name in names):
        raise _UnsupportedSource("function names cannot replace allocators, main or reserved identifiers")
    graph = {name: set() for name in names}
    allowed = {
        c_ast.FileAST, c_ast.FuncDef, c_ast.FuncDecl, c_ast.Decl, c_ast.TypeDecl,
        c_ast.IdentifierType, c_ast.PtrDecl, c_ast.ParamList, c_ast.Typename,
        c_ast.Compound, c_ast.DeclList, c_ast.If, c_ast.While, c_ast.DoWhile,
        c_ast.For, c_ast.Break, c_ast.Continue, c_ast.Return, c_ast.EmptyStatement,
        c_ast.Assignment, c_ast.BinaryOp, c_ast.UnaryOp, c_ast.TernaryOp,
        c_ast.Constant, c_ast.ID, c_ast.FuncCall, c_ast.ExprList, c_ast.Cast,
    }
    count = 0
    for definition in tree.ext:
        declaration = definition.decl
        signature = declaration.type
        if not isinstance(signature, c_ast.FuncDecl):
            raise _UnsupportedSource("functions require explicit prototypes")
        _type(c_ast, signature.type, allow_void=True)
        if signature.args is None:
            raise _UnsupportedSource("functions require an explicit parameter list")
        parameters = signature.args.params
        if (len(parameters) == 1 and isinstance(parameters[0], c_ast.Typename)
                and _type(c_ast, parameters[0].type, allow_void=True) == "void"):
            parameters = []
        if len(parameters) > 8:
            raise _UnsupportedSource("functions support at most eight parameters")
        for parameter in parameters:
            if not isinstance(parameter, c_ast.Decl) or not _identifier(parameter.name):
                raise _UnsupportedSource("parameters must be named supported values")
            kind = _type(c_ast, parameter.type)
            if declaration.name == function and kind == "pointer":
                raise _UnsupportedSource("entry parameters must be scalar integers or booleans")
        parameter_ids = {id(parameter) for parameter in parameters}
        stack = [(definition, 1, None, "")]
        declarations = 0
        while stack:
            node, depth, parent, slot = stack.pop()
            count += 1
            if count > 4_096 or depth > 64 or type(node) not in allowed:
                raise _UnsupportedSource("heap source exceeds its syntax bounds or uses unsupported syntax")
            if isinstance(node, c_ast.Decl):
                if (not _identifier(node.name) or node.storage or node.funcspec or node.quals
                        or node.align or node.bitsize):
                    raise _UnsupportedSource("declarations must be named and unqualified without storage attributes")
                if node is not declaration:
                    if node.name in reserved or node.name in names:
                        raise _UnsupportedSource("local declarations cannot shadow function or allocator names")
                    _type(c_ast, node.type)
                    if id(node) not in parameter_ids:
                        declarations += 1
                        if declarations > 64 or node.init is None:
                            raise _UnsupportedSource("at most 64 explicitly initialized locals are supported per function")
                        if _contains_identifier(c_ast, node.init, node.name):
                            raise _UnsupportedSource("local initializers cannot read the variable being declared")
            if isinstance(node, c_ast.Typename):
                _type(c_ast, node.type, allow_void=True)
            if isinstance(node, c_ast.PtrDecl):
                _type(c_ast, node)
            if isinstance(node, c_ast.ID) and not _identifier(node.name):
                raise _UnsupportedSource("reserved intrinsic identifiers are unsupported")
            if isinstance(node, c_ast.Constant):
                if node.type not in {"int", "unsigned int"} or len(node.value) > 32:
                    raise _UnsupportedSource("only bounded integer constants are supported")
            if isinstance(node, c_ast.UnaryOp) and node.op not in {
                    "+", "-", "!", "~", "*", "&", "sizeof", "++", "--", "p++", "p--"}:
                raise _UnsupportedSource("unary operator is unsupported")
            mutation = isinstance(node, c_ast.Assignment) or (
                isinstance(node, c_ast.UnaryOp) and node.op in {"++", "--", "p++", "p--"})
            statement = isinstance(parent, c_ast.Compound) or (
                isinstance(parent, c_ast.For) and slot in {"init", "next", "stmt"}) or (
                isinstance(parent, (c_ast.If, c_ast.While, c_ast.DoWhile))
                and slot in {"iftrue", "iffalse", "stmt"})
            if mutation and not statement:
                raise _UnsupportedSource("mutations must be standalone statements or for updates")
            if isinstance(node, c_ast.FuncCall):
                if not isinstance(node.name, c_ast.ID) or node.name.name not in {*names, "malloc", "free"}:
                    raise _UnsupportedSource("calls must name a captured function or a protected allocator")
                call_statement = statement or (
                    isinstance(parent, c_ast.Decl) and slot == "init") or (
                    isinstance(parent, c_ast.Assignment) and slot == "rvalue") or (
                    isinstance(parent, c_ast.Return) and slot == "expr")
                if not call_statement:
                    raise _UnsupportedSource("calls must be standalone or the entire initializer, assignment value or return")
                arguments = node.args.exprs if node.args is not None else []
                if node.name.name == "malloc":
                    if len(arguments) != 1 or not 1 <= _allocation_size(c_ast, arguments[0]) <= MAX_ALLOCATION_BYTES:
                        raise _UnsupportedSource("malloc needs a fixed size between one and 4096 bytes")
                elif node.name.name == "free":
                    if len(arguments) != 1:
                        raise _UnsupportedSource("free requires one argument")
                else:
                    graph[declaration.name].add(node.name.name)
            stack.extend((child, depth + 1, node, name) for name, child in node.children())
    _acyclic(graph)
    generator = c_generator.CGenerator()
    prototypes = "\n".join(generator.visit(item.decl) + ";" for item in tree.ext)
    prelude = "void *malloc(unsigned long);\nvoid free(void *);\n"
    generated = (prelude + prototypes + "\n" + generator.visit(tree)).encode("utf-8")
    if len(generated) > 65_536:
        raise _UnsupportedSource("normalized heap source exceeds its byte limit")
    return generated


def _identifier(name: Any) -> bool:
    return isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is not None


def _type(c_ast: Any, node: Any, *, allow_void: bool = False) -> str:
    if isinstance(node, c_ast.PtrDecl):
        if node.quals or isinstance(node.type, c_ast.PtrDecl):
            raise _UnsupportedSource("only unqualified single-level pointers are supported")
        _scalar_type(c_ast, node.type, allow_void=True)
        return "pointer"
    names = _scalar_type(c_ast, node, allow_void=allow_void)
    return "void" if names == ("void",) else "scalar"


def _contains_identifier(c_ast: Any, node: Any, name: str) -> bool:
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, c_ast.ID) and current.name == name:
            return True
        pending.extend(child for _, child in current.children())
    return False


def _allocation_size(c_ast: Any, node: Any) -> int:
    if isinstance(node, c_ast.Constant) and re.fullmatch(r"[0-9]{1,5}", node.value):
        return int(node.value)
    if isinstance(node, c_ast.UnaryOp) and node.op == "sizeof" and isinstance(node.expr, c_ast.Typename):
        kind = _scalar_type(c_ast, node.expr.type)
        return 1 if kind == ("_Bool",) else 4
    if isinstance(node, c_ast.BinaryOp) and node.op in {"+", "*"}:
        left, right = _allocation_size(c_ast, node.left), _allocation_size(c_ast, node.right)
        result = left + right if node.op == "+" else left * right
        if result <= MAX_ALLOCATION_BYTES:
            return result
    raise _UnsupportedSource("allocation sizes must be fixed literals or supported scalar sizeof expressions")


def _acyclic(graph: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise _UnsupportedSource("recursive call graphs are unsupported")
        if name in visited:
            return
        visiting.add(name)
        for target in graph[name]:
            visit(target)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)
