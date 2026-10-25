from pathlib import Path

import pytest

from scripts.qualify_refinedrust import read_axioms, read_checker_safety


def test_assumption_reader_preserves_qualified_identities(tmp_path: Path) -> None:
    report = tmp_path / "assumptions.out"
    report.write_text(
        "Axioms:\n"
        "Stdlib.Logic.ProofIrrelevance.proof_irrelevance :\n"
        "  forall (P : Prop) (p1 p2 : P), p1 = p2\n"
        "Stdlib.Logic.FunctionalExtensionality.functional_extensionality_dep :\n"
        "  forall (A : Type) (B : A -> Type) (f g : forall x : A, B x),\n"
        "    (forall x : A, f x = g x) -> f = g\n"
        "producer.ProofIrrelevance.proof_irrelevance : False\n",
        encoding="utf-8",
    )
    observed = read_axioms(report)
    assert set(observed) == {
        "Stdlib.Logic.ProofIrrelevance.proof_irrelevance",
        "Stdlib.Logic.FunctionalExtensionality.functional_extensionality_dep",
        "producer.ProofIrrelevance.proof_irrelevance",
    }
    assert observed["producer.ProofIrrelevance.proof_irrelevance"]["display_type"] == "False"


def test_closed_assumption_context(tmp_path: Path) -> None:
    report = tmp_path / "assumptions.out"
    report.write_text("Closed under the global context\n", encoding="utf-8")
    assert read_axioms(report) == {}


@pytest.mark.parametrize("content", [
    "",
    "Warning: proof was not loaded\n",
    "Axioms:\n",
    "Axioms:\nproof_irrelevance : True\n",
    "Axioms:\nProducer.hole :\n",
    "Axioms:\n  forall P, P\n",
    "Axioms:\nProducer.hole : True\nProducer.hole : True\n",
    "Axioms:\nProducer.hole : True\nTheory: Type-in-Type\n",
    "Closed under the global context\nAxioms:\nProducer.hole : True\n",
])
def test_incomplete_or_unrecognized_assumptions_block(tmp_path: Path, content: str) -> None:
    report = tmp_path / "assumptions.out"
    report.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        read_axioms(report)


SAFE_CHECKER_REPORT = """* Theory: Set is predicative
* Theory: Rewrite rules are not allowed
* Axioms:
    Stdlib.Logic.ProofIrrelevance.proof_irrelevance
* Constants/Inductives relying on type-in-type: <none>
* Constants/Inductives relying on unsafe (co)fixpoints: <none>
* Inductives whose positivity is assumed: <none>
"""


def test_recursive_checker_safe_summary(tmp_path: Path) -> None:
    report = tmp_path / "replay.log"
    report.write_text(SAFE_CHECKER_REPORT, encoding="utf-8")
    assert read_checker_safety(report)["safe"] is True


def test_recursive_checker_closed_summary(tmp_path: Path) -> None:
    report = tmp_path / "replay.log"
    report.write_text(SAFE_CHECKER_REPORT.replace(
        "* Axioms:\n    Stdlib.Logic.ProofIrrelevance.proof_irrelevance",
        "* Axioms: <none>"), encoding="utf-8")
    assert read_checker_safety(report)["safe"] is True


def test_recursive_checker_rejects_content_under_closed_axioms(tmp_path: Path) -> None:
    report = tmp_path / "replay.log"
    report.write_text(SAFE_CHECKER_REPORT.replace(
        "* Axioms:", "* Axioms: <none>"), encoding="utf-8")
    assert read_checker_safety(report)["safe"] is False


@pytest.mark.parametrize("content", [
    "",
    SAFE_CHECKER_REPORT.replace("* Theory: Set is predicative\n", ""),
    SAFE_CHECKER_REPORT + "* Theory: Set is predicative\n",
    SAFE_CHECKER_REPORT + "* Unknown trust flag: <none>\n",
    SAFE_CHECKER_REPORT.replace("* Theory: Set is predicative", "* Theory: Set is impredicative"),
    SAFE_CHECKER_REPORT.replace("* Theory: Rewrite rules are not allowed", "* Theory: Rewrite rules are allowed"),
    SAFE_CHECKER_REPORT.replace("type-in-type: <none>", "type-in-type:\n    Producer.bad"),
    SAFE_CHECKER_REPORT.replace("unsafe (co)fixpoints: <none>", "unsafe (co)fixpoints:\n    Producer.loop"),
    SAFE_CHECKER_REPORT.replace("unsafe (co)fixpoints: <none>", "unsafe (co)fixpoints: <none>\n    Producer.loop"),
    SAFE_CHECKER_REPORT.replace("positivity is assumed: <none>", "positivity is assumed:\n    Producer.bad"),
])
def test_recursive_checker_requires_all_safe_flags(tmp_path: Path, content: str) -> None:
    report = tmp_path / "replay.log"
    report.write_text(content, encoding="utf-8")
    assert read_checker_safety(report)["safe"] is False


def test_recursive_checker_records_unsafe_declaration(tmp_path: Path) -> None:
    report = tmp_path / "replay.log"
    report.write_text(SAFE_CHECKER_REPORT.replace(
        "unsafe (co)fixpoints: <none>", "unsafe (co)fixpoints:\n    Producer.false_proof"),
        encoding="utf-8")
    assert read_checker_safety(report)["unsafe_fixpoints"] == ["Producer.false_proof"]
