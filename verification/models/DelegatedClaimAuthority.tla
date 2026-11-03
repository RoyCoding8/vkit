---- MODULE DelegatedClaimAuthority ----
EXTENDS Naturals

CONSTANTS Mutant, Fault

VARIABLES phase, targetTrusted, targetIdentity, entailment, strictStrengthening,
          completeSatisfaction, nearMissRejected, receiptGeneration,
          acceptedReceiptGeneration, cancelled, proofScope, sampled,
          basisClaim, currentClaim, acceptedClaim, accepted,
          codeApproved, codeApprovalAtClaimAcceptance

vars == <<phase, targetTrusted, targetIdentity, entailment, strictStrengthening,
          completeSatisfaction, nearMissRejected, receiptGeneration,
          acceptedReceiptGeneration, cancelled, proofScope, sampled,
          basisClaim, currentClaim, acceptedClaim, accepted,
          codeApproved, codeApprovalAtClaimAcceptance>>

Init ==
    /\ phase = "requested"
    /\ targetTrusted = (Fault # "untrusted_target")
    /\ targetIdentity = (Fault # "wrong_target_identity")
    /\ entailment = (Fault # "missing_entailment")
    /\ strictStrengthening = (Fault # "missing_strict_strengthening")
    /\ completeSatisfaction = (Fault # "sample_only_satisfaction")
    /\ nearMissRejected = (Fault # "missing_near_miss_rejection")
    /\ receiptGeneration = 0
    /\ acceptedReceiptGeneration = 0
    /\ cancelled = FALSE
    /\ proofScope = IF Fault = "wrong_model_scope" THEN "native" ELSE "restricted_captured_rust_kani_model"
    /\ sampled = FALSE
    /\ basisClaim = IF Fault = "self_justifying_basis" THEN "current_claim" ELSE "trusted_prior_claim"
    /\ currentClaim = "current_claim"
    /\ acceptedClaim = "none"
    /\ accepted = FALSE
    /\ codeApproved = FALSE
    /\ codeApprovalAtClaimAcceptance = FALSE

Prepare ==
    /\ phase = "requested"
    /\ phase' = "prepared"
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, receiptGeneration,
                    acceptedReceiptGeneration, cancelled, proofScope, sampled,
                    basisClaim, currentClaim, acceptedClaim, accepted,
                    codeApproved, codeApprovalAtClaimAcceptance>>

SampleCandidate ==
    /\ phase = "prepared"
    /\ phase' = "sampled"
    /\ sampled' = TRUE
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, receiptGeneration,
                    acceptedReceiptGeneration, cancelled, proofScope,
                    basisClaim, currentClaim, acceptedClaim, accepted,
                    codeApproved, codeApprovalAtClaimAcceptance>>

RunIndependentProofs ==
    /\ phase \in {"prepared", "sampled"}
    /\ phase' = "proved"
    /\ receiptGeneration' = receiptGeneration + 1
    /\ acceptedReceiptGeneration' = receiptGeneration + 1
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, cancelled, proofScope,
                    sampled, basisClaim, currentClaim, acceptedClaim, accepted,
                    codeApproved, codeApprovalAtClaimAcceptance>>

CancelProofs ==
    /\ phase = "proved"
    /\ phase' = "cancelled"
    /\ receiptGeneration' = receiptGeneration + 1
    /\ cancelled' = TRUE
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, acceptedReceiptGeneration,
                    proofScope, sampled, basisClaim, currentClaim, acceptedClaim,
                    accepted, codeApproved, codeApprovalAtClaimAcceptance>>

AcceptanceGuards ==
    /\ (Mutant = "omit_target_basis_identity" \/ (targetTrusted /\ targetIdentity))
    /\ (Mutant = "omit_entailment" \/ entailment)
    /\ (Mutant = "omit_strict_strengthening" \/ strictStrengthening)
    /\ (Mutant \in {"omit_complete_satisfaction", "sample_authorizes_claim"}
         \/ completeSatisfaction)
    /\ (Mutant = "omit_meaningful_near_miss" \/ nearMissRejected)
    /\ (Mutant = "omit_fresh_receipt_cancel" \/
         (acceptedReceiptGeneration = receiptGeneration /\ ~cancelled))
    /\ (Mutant = "omit_model_scope" \/ proofScope = "restricted_captured_rust_kani_model")
    /\ (Mutant = "omit_prior_claim_identity" \/ basisClaim = "trusted_prior_claim")

AcceptClaim ==
    /\ phase \in {"proved", "cancelled"}
    /\ ~accepted
    /\ (Mutant # "sample_authorizes_claim" \/ sampled)
    /\ AcceptanceGuards
    /\ accepted' = TRUE
    /\ phase' = "accepted"
    /\ acceptedClaim' = currentClaim
    /\ codeApproved' = IF Mutant = "claim_approves_code" THEN TRUE ELSE codeApproved
    /\ codeApprovalAtClaimAcceptance' = codeApproved
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, receiptGeneration,
                    acceptedReceiptGeneration, cancelled, proofScope, sampled,
                    basisClaim, currentClaim>>

ApproveCode ==
    /\ phase = "proved"
    /\ phase' = "proved"
    /\ codeApproved' = TRUE
    /\ UNCHANGED <<targetTrusted, targetIdentity, entailment, strictStrengthening,
                    completeSatisfaction, nearMissRejected, receiptGeneration,
                    acceptedReceiptGeneration, cancelled, proofScope, sampled,
                    basisClaim, currentClaim, acceptedClaim, accepted,
                    codeApprovalAtClaimAcceptance>>

Next == Prepare \/ SampleCandidate \/ RunIndependentProofs \/ CancelProofs \/ AcceptClaim \/ ApproveCode
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"requested", "prepared", "sampled", "proved", "cancelled", "accepted"}
    /\ targetTrusted \in BOOLEAN
    /\ targetIdentity \in BOOLEAN
    /\ entailment \in BOOLEAN
    /\ strictStrengthening \in BOOLEAN
    /\ completeSatisfaction \in BOOLEAN
    /\ nearMissRejected \in BOOLEAN
    /\ receiptGeneration \in {0, 1, 2}
    /\ acceptedReceiptGeneration \in {0, 1}
    /\ cancelled \in BOOLEAN
    /\ proofScope \in {"restricted_captured_rust_kani_model", "native"}
    /\ sampled \in BOOLEAN
    /\ basisClaim \in {"trusted_prior_claim", "current_claim"}
    /\ currentClaim = "current_claim"
    /\ acceptedClaim \in {"none", "current_claim"}
    /\ accepted \in BOOLEAN
    /\ codeApproved \in BOOLEAN
    /\ codeApprovalAtClaimAcceptance \in BOOLEAN

AcceptedHasTrustedTargetBasis == ~accepted \/ (targetTrusted /\ targetIdentity)
AcceptedHasEntailment == ~accepted \/ entailment
AcceptedIsStrictStrengthening == ~accepted \/ strictStrengthening
AcceptedHasCompleteSatisfaction == ~accepted \/ completeSatisfaction
AcceptedHasRejectedMeaningfulNearMiss == ~accepted \/ nearMissRejected
AcceptedHasFreshUncancelledReceipt ==
    ~accepted \/ (acceptedReceiptGeneration = receiptGeneration /\ ~cancelled)
AcceptedUsesRestrictedModel ==
    ~accepted \/ proofScope = "restricted_captured_rust_kani_model"
SamplesCannotAuthorize == ~(sampled /\ ~completeSatisfaction /\ accepted)
ClaimAcceptanceDoesNotApproveCode ==
    ~accepted \/ codeApprovalAtClaimAcceptance = codeApproved
CurrentClaimCannotBeItsOwnBasis ==
    ~accepted \/ (basisClaim = "trusted_prior_claim" /\ acceptedClaim # basisClaim)

====
