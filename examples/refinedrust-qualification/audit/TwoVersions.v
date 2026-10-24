From VKitQualification Require Contract.
From VKitVersions.Old.vkit_qualification.proofs Require proof_reserve.
From VKitVersions.New.vkit_qualification.proofs Require proof_reserve.
From refinedrust Require Import typing.

Set Default Proof Using "Type".

Section binding.
Context `{RRGS : !refinedrustGS Σ}.

Theorem both_bodies (π : thread_id) :
  VKitQualification.Contract.expected_body (RRGS:=RRGS)
    VKitVersions.Old.vkit_qualification.generated.generated_code_vkit_qualification.reserve_def π ∧
  VKitQualification.Contract.expected_body (RRGS:=RRGS)
    VKitVersions.New.vkit_qualification.generated.generated_code_vkit_qualification.reserve_def π.
Proof.
  split.
  - exact (VKitVersions.Old.vkit_qualification.proofs.proof_reserve.reserve_proof π).
  - exact (VKitVersions.New.vkit_qualification.proofs.proof_reserve.reserve_proof π).
Qed.

End binding.

Set Printing Fully Qualified.
Unset Printing Notations.
Set Printing Width 1000000.
Redirect "../audit/two-version-assumptions" Print Assumptions both_bodies.
