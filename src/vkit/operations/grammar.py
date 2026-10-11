"""Find independently checked ambiguity witnesses in a captured literal CFG corpus."""
from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ._data import read_json

BACKEND_VERSION = "1.3.1"
MODEL_SCOPE = (
    "Ambiguity of supplied strings under a captured context-free grammar with literal terminals. "
    "Two distinct derivation trees are checked against the productions and string. A negative "
    "answer concerns the supplied corpus only, not global grammar unambiguity."
)


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"version", "start", "productions", "samples"}:
        raise ValueError("grammar bundle needs exactly version, start, productions and samples")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("version must be 1")
    productions = raw["productions"]
    samples = raw["samples"]
    if not isinstance(productions, list) or not 1 <= len(productions) <= 64:
        raise ValueError("supply 1 to 64 productions")
    if (not isinstance(samples, list) or not 1 <= len(samples) <= 64
            or any(not isinstance(s, str) or len(s) > 256 for s in samples)):
        raise ValueError("supply 1 to 64 sample strings, each at most 256 characters")
    names: set[str] = set()
    signatures: set[str] = set()
    symbols = 0
    for production in productions:
        if (not isinstance(production, dict) or set(production) != {"left", "right"}
                or not isinstance(production["left"], str) or not production["left"]
                or len(production["left"]) > 64 or not isinstance(production["right"], list)):
            raise ValueError("each production needs a nonempty left name and right symbol array")
        names.add(production["left"])
        symbols += len(production["right"])
        for symbol in production["right"]:
            if (not isinstance(symbol, dict) or len(symbol) != 1
                    or next(iter(symbol)) not in {"literal", "nonterminal"}):
                raise ValueError("symbols are either {literal: text} or {nonterminal: name}")
            value = next(iter(symbol.values()))
            if not isinstance(value, str) or not value or len(value) > 64:
                raise ValueError("symbols must contain nonempty strings of at most 64 characters")
        signature = json.dumps(production, sort_keys=True, ensure_ascii=True)
        if signature in signatures:
            raise ValueError("duplicate productions are unsupported")
        signatures.add(signature)
    if symbols > 256 or len(names) > 32:
        raise ValueError("grammar exceeds 256 symbols or 32 nonterminals")
    if not isinstance(raw["start"], str) or raw["start"] not in names:
        raise ValueError("start must name a declared nonterminal")
    for production in productions:
        if any(s["nonterminal"] not in names for s in production["right"] if "nonterminal" in s):
            raise ValueError("undeclared nonterminal")

    nullable: set[str] = set()
    while True:
        found = {p["left"] for p in productions
                 if all(s.get("nonterminal") in nullable for s in p["right"])}
        if found <= nullable:
            break
        nullable |= found
    zero_yield_edges = {name: set() for name in names}
    for production in productions:
        right = production["right"]
        for i, symbol in enumerate(right):
            if "nonterminal" in symbol and all(
                    s.get("nonterminal") in nullable for j, s in enumerate(right) if i != j):
                zero_yield_edges[production["left"]].add(symbol["nonterminal"])
    def acyclic(name: str, visiting: set[str], visited: set[str]) -> None:
        if name in visiting:
            raise ValueError("zero-yield grammar cycles are unsupported")
        if name in visited:
            return
        visiting.add(name)
        for child in zero_yield_edges[name]:
            acyclic(child, visiting, visited)
        visiting.remove(name)
        visited.add(name)
    visited: set[str] = set()
    for name in names:
        acyclic(name, set(), visited)
    return raw


def _lark_grammar(bundle: dict[str, Any]) -> tuple[str, str]:
    names = {name: f"r{i}" for i, name in enumerate(sorted({p["left"] for p in bundle["productions"]}))}
    grouped: dict[str, list[str]] = {name: [] for name in names}
    for i, production in enumerate(bundle["productions"]):
        rhs = " ".join(names[s["nonterminal"]] if "nonterminal" in s else
                       json.dumps(s["literal"], ensure_ascii=True) for s in production["right"])
        grouped[production["left"]].append(f"{rhs} -> p{i}")
    grammar = "\n".join(names[name] + ": " + "\n    | ".join(alternatives)
                        for name, alternatives in grouped.items())
    return grammar, names[bundle["start"]]


def _derivations(tree: Any, budget: list[int]) -> list[Any]:
    from lark import Token

    budget[0] -= 1
    if budget[0] < 0:
        raise OverflowError("parse forest exceeded witness traversal limit")
    if isinstance(tree, Token):
        return [str(tree)]
    if str(tree.data) == "_ambig":
        answers = []
        for child in tree.children:
            for result in _derivations(child, budget):
                if result not in answers:
                    answers.append(result)
                if len(answers) == 2:
                    return answers
        return answers
    children = [_derivations(child, budget) for child in tree.children]
    first = {"production": int(str(tree.data)[1:]), "children": [c[0] for c in children]}
    answers = [first]
    for i, options in enumerate(children):
        if len(options) > 1:
            other = list(first["children"])
            other[i] = options[1]
            answers.append({"production": first["production"], "children": other})
            break
    return answers


def _check_witness(bundle: dict[str, Any], tree: Any, expected: str) -> str:
    if not isinstance(tree, dict) or set(tree) != {"production", "children"}:
        raise ValueError("invalid derivation node")
    index = tree["production"]
    if type(index) is not int or not 0 <= index < len(bundle["productions"]):
        raise ValueError("unknown production")
    production = bundle["productions"][index]
    children = tree["children"]
    if production["left"] != expected or not isinstance(children, list) or len(children) != len(production["right"]):
        raise ValueError("derivation does not match production")
    text = []
    for symbol, child in zip(production["right"], children):
        if "literal" in symbol:
            if child != symbol["literal"]:
                raise ValueError("derivation terminal differs")
            text.append(child)
        else:
            text.append(_check_witness(bundle, child, symbol["nonterminal"]))
    return "".join(text)


def _analyze(bundle: dict[str, Any]) -> dict[str, Any]:
    try:
        installed = version("lark")
    except PackageNotFoundError:
        return {"status": "UNAVAILABLE", "reason": "lark 1.3.1 is not installed"}
    if installed != BACKEND_VERSION:
        return {"status": "UNAVAILABLE", "reason": f"requires lark {BACKEND_VERSION}; found {installed}"}
    from lark import Lark, UnexpectedInput

    grammar, start = _lark_grammar(bundle)
    parser = Lark(grammar, start=start, parser="earley", lexer="dynamic_complete",
                  ambiguity="explicit", keep_all_tokens=True, maybe_placeholders=False)
    rejected = []
    for i, sample in enumerate(bundle["samples"]):
        try:
            forest = parser.parse(sample)
        except UnexpectedInput:
            rejected.append(i)
            continue
        trees = _derivations(forest, [10_000])
        if len(trees) > 1:
            if (trees[0] == trees[1] or any(_check_witness(bundle, tree, bundle["start"]) != sample for tree in trees)):
                return {"status": "UNKNOWN", "reason": "independent derivation validation failed"}
            return {"status": "AMBIGUOUS", "sample_index": i, "sample": sample, "trees": trees,
                    "witness_checked": True}
    return {"status": "NO_AMBIGUITY_IN_CORPUS", "sample_count": len(bundle["samples"]),
            "rejected_sample_indices": rejected}


def find_grammar_ambiguity(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be an integer from 1 to 60")
    raw, digest = read_json(root, path, max_bytes=128_000, reject_symlinks=True)
    try:
        bundle = _validate(raw)
    except ValueError as exc:
        result = {"status": "UNSUPPORTED", "reason": str(exc)}
    else:
        try:
            process = subprocess.run([sys.executable, "-I", "-m", "vkit.operations.grammar", "--worker"],
                                     input=json.dumps(bundle, ensure_ascii=True), capture_output=True,
                                     encoding="utf-8", timeout=timeout_seconds, check=False)
            if process.returncode != 0 or len(process.stdout) > 512_000:
                result = {"status": "UNKNOWN", "reason": "grammar worker failed or exceeded output limit"}
            else:
                result = json.loads(process.stdout)
                if not isinstance(result, dict) or result.get("status") not in {
                        "AMBIGUOUS", "NO_AMBIGUITY_IN_CORPUS", "UNKNOWN", "UNAVAILABLE"}:
                    result = {"status": "UNKNOWN", "reason": "invalid grammar worker response"}
                elif result["status"] == "AMBIGUOUS":
                    trees = result["trees"]
                    sample = bundle["samples"][result["sample_index"]]
                    if (len(trees) != 2 or trees[0] == trees[1] or result["sample"] != sample
                            or any(_check_witness(bundle, tree, bundle["start"]) != sample for tree in trees)):
                        result = {"status": "UNKNOWN", "reason": "invalid ambiguity witness"}
        except subprocess.TimeoutExpired:
            result = {"status": "UNKNOWN", "reason": "grammar worker exceeded its deadline"}
        except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError):
            result = {"status": "UNKNOWN", "reason": "grammar worker could not return a checked answer"}
    return {"operation": "find_grammar_ambiguity", "path": path, "source_sha256": digest,
            "backend": "lark", "backend_version": BACKEND_VERSION, "model_scope": MODEL_SCOPE, **result}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    try:
        answer = _analyze(_validate(json.load(sys.stdin)))
    except Exception as error:
        answer = {"status": "UNKNOWN", "reason": f"grammar analysis failed: {type(error).__name__}"}
    print(json.dumps(answer, ensure_ascii=True))
