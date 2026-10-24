from pathlib import Path

import pytest

from scripts.qualify_refinedrust import read_axioms


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
