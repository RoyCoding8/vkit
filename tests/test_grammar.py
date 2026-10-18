from __future__ import annotations

import json
import subprocess

import pytest

from vkit.operations import grammar


def _bundle(productions, samples, start="s"):
    return {"version": 1, "start": start, "productions": productions, "samples": samples}


def _rule(left, *symbols):
    return {"left": left, "right": list(symbols)}


def _run(tmp_path, bundle):
    (tmp_path / "grammar.json").write_text(json.dumps(bundle), encoding="utf-8")
    return grammar.find_grammar_ambiguity(tmp_path, "grammar.json")


def test_two_associations_have_distinct_checked_derivations(tmp_path):
    bundle = _bundle([_rule("s", {"nonterminal": "s"}, {"literal": "+"}, {"nonterminal": "s"}),
                      _rule("s", {"literal": "a"})], ["a+a", "a+a+a"])
    result = _run(tmp_path, bundle)
    assert result["status"] == "AMBIGUOUS"
    assert result["sample_index"] == 1
    assert result["witness_checked"] is True
    assert result["trees"][0] != result["trees"][1]
    assert [grammar._check_witness(bundle, tree, "s") for tree in result["trees"]] == ["a+a+a", "a+a+a"]


def test_lexical_segmentation_ambiguity_is_preserved(tmp_path):
    bundle = _bundle([_rule("s", {"literal": "aa"}), _rule("s", {"literal": "a"}, {"literal": "a"})], ["aa"])
    result = _run(tmp_path, bundle)
    assert result["status"] == "AMBIGUOUS"
    assert {tree["production"] for tree in result["trees"]} == {0, 1}


def test_negative_answer_is_only_about_corpus_and_records_rejected_strings(tmp_path):
    result = _run(tmp_path, _bundle([_rule("s", {"literal": "a"})], ["a", "b"]))
    assert result["status"] == "NO_AMBIGUITY_IN_CORPUS"
    assert result["sample_count"] == 2
    assert result["rejected_sample_indices"] == [1]


def test_empty_production_and_unicode_literal_are_supported(tmp_path):
    result = _run(tmp_path, _bundle([_rule("s"), _rule("s", {"literal": "λ\n"})], ["", "λ\n"]))
    assert result["status"] == "NO_AMBIGUITY_IN_CORPUS"
    assert result["rejected_sample_indices"] == []


@pytest.mark.parametrize("productions", [
    [_rule("s", {"nonterminal": "s"}), _rule("s", {"literal": "a"})],
    [_rule("s", {"nonterminal": "missing"})],
    [_rule("s", {"literal": ""})],
    [_rule("s", {"literal": "a"}), _rule("s", {"literal": "a"})],
    [_rule("s", {"regex": ".*"})],
])
def test_rejects_outside_profile_before_parser(tmp_path, productions):
    assert _run(tmp_path, _bundle(productions, ["a"]))["status"] == "UNSUPPORTED"


def test_grammar_names_and_literals_are_data_not_lark_directives(tmp_path):
    literal = '%import "/private/file"'
    result = _run(tmp_path, _bundle([_rule("%import anything", {"literal": literal})],
                                  [literal], start="%import anything"))
    assert result["status"] == "NO_AMBIGUITY_IN_CORPUS"
    assert result["rejected_sample_indices"] == []


def test_changed_derivation_is_rejected_by_independent_checker():
    bundle = _bundle([_rule("s", {"literal": "a"})], ["a"])
    with pytest.raises(ValueError, match="terminal differs"):
        grammar._check_witness(bundle, {"production": 0, "children": ["b"]}, "s")


def test_deadline_does_not_become_negative_answer(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 1)
    monkeypatch.setattr(grammar.subprocess, "run", timeout)
    assert _run(tmp_path, _bundle([_rule("s", {"literal": "a"})], ["a"]))["status"] == "UNKNOWN"
