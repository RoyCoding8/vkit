From VKitQualification Require Contract.
From refinedrust.examples.vkit_qualification.proofs Require proof_reserve.
From refinedrust Require Import typing.

Set Default Proof Using "Type".

Section binding.
Context `{RRGS : !refinedrustGS Σ}.
Theorem reserve_expected (π : thread_id) :
  VKitQualification.Contract.expected_body (RRGS:=RRGS) π.
Proof. exact (refinedrust.examples.vkit_qualification.proofs.proof_reserve.reserve_proof π). Qed.
End binding.

Set Printing Fully Qualified.
Unset Printing Notations.
Set Printing Width 1000000.
Redirect "../audit/assumptions" Print Assumptions reserve_expected.
