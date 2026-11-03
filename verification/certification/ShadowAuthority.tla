---- MODULE ShadowAuthority ----
EXTENDS FiniteSets

CONSTANT Fault

Kinds == {"registered_check", "formal_goal", "change_certification"}
Ids == {"shared-id"}
Results == {"none", "PASS", "FAIL", "BLOCKED"}
Assurances == {"none", "empirical", "finite_model", "trusted_engine"}
Claims == {"none", "established", "refuted", "unknown"}
Faults == {"none", "verus_check_none", "shadow_established", "gate_requires_shadow", "gate_accepts_shadow"}

RegisteredSlot == "registered_check:shared-id"
FormalSlot == "formal_goal:shared-id"
ShadowSlot == "change_certification:shared-id"
Slots == {RegisteredSlot, FormalSlot, ShadowSlot}
RequiredSlots == {RegisteredSlot, FormalSlot}
SlotKind(slot) ==
    IF slot = RegisteredSlot THEN "registered_check"
    ELSE IF slot = FormalSlot THEN "formal_goal" ELSE "change_certification"
SlotId(slot) == "shared-id"

EmptyReceipt == [kind |-> "none", id |-> "none", result |-> "none",
                 assurance |-> "none", claim_outcome |-> "none",
                 source_proof |-> FALSE, approval_authority |-> FALSE]

VARIABLES receipts, gate_ready
vars == <<receipts, gate_ready>>

Init ==
    /\ receipts = [s \in Slots |-> EmptyReceipt]
    /\ gate_ready = FALSE

ClaimFor(result) == IF result = "PASS" THEN "established"
                    ELSE IF result = "FAIL" THEN "refuted" ELSE "unknown"

RequiredReceipt(slot, result) ==
    [kind |-> SlotKind(slot), id |-> SlotId(slot), result |-> result,
     assurance |-> IF SlotKind(slot) = "registered_check" THEN "empirical"
                   ELSE "trusted_engine",
     claim_outcome |-> ClaimFor(result),
     source_proof |-> FALSE, approval_authority |-> FALSE]

ShadowReceipt(result) ==
    [kind |-> "change_certification", id |-> "shared-id", result |-> result,
     assurance |-> IF Fault = "verus_check_none" THEN "trusted_engine" ELSE "empirical",
     claim_outcome |-> IF Fault = "shadow_established" THEN ClaimFor(result) ELSE "unknown",
     source_proof |-> FALSE, approval_authority |-> FALSE]

RequiredPassIn(records, slot) ==
    /\ records[slot].kind = SlotKind(slot)
    /\ records[slot].id = SlotId(slot)
    /\ records[slot].result = "PASS"
    /\ records[slot].assurance = IF SlotKind(slot) = "registered_check" THEN "empirical" ELSE "trusted_engine"
    /\ records[slot].claim_outcome = "established"

RequiredGateIn(records) == \A slot \in RequiredSlots : RequiredPassIn(records, slot)

GateDecision(records) ==
    IF Fault = "gate_requires_shadow" /\ records[ShadowSlot] # EmptyReceipt
       THEN RequiredGateIn(records) /\ records[ShadowSlot].result = "PASS"
    ELSE IF Fault = "gate_accepts_shadow" /\ records[ShadowSlot] # EmptyReceipt
       THEN RequiredGateIn(records) \/ records[ShadowSlot].result = "PASS"
       ELSE RequiredGateIn(records)

PublishRequired(slot, result) ==
    /\ slot \in RequiredSlots
    /\ result \in {"PASS", "FAIL", "BLOCKED"}
    /\ receipts[slot] = EmptyReceipt
    /\ receipts' = [receipts EXCEPT ![slot] = RequiredReceipt(slot, result)]
    /\ gate_ready' = GateDecision(receipts')

PublishShadow(result) ==
    /\ result \in {"PASS", "FAIL", "BLOCKED"}
    /\ receipts[ShadowSlot] = EmptyReceipt
    /\ receipts' = [receipts EXCEPT ![ShadowSlot] = ShadowReceipt(result)]
    /\ gate_ready' = GateDecision(receipts')

RequiredGate == RequiredGateIn(receipts)

Done == UNCHANGED vars
Next ==
    \/ \E slot \in RequiredSlots : \E result \in {"PASS", "FAIL", "BLOCKED"} : PublishRequired(slot, result)
    \/ \E result \in {"PASS", "FAIL", "BLOCKED"} : PublishShadow(result)
    \/ Done

Spec == Init /\ [][Next]_vars

ReceiptType == [kind : Kinds \cup {"none"}, id : Ids \cup {"none"},
                result : Results, assurance : Assurances, claim_outcome : Claims,
                source_proof : BOOLEAN, approval_authority : BOOLEAN]

TypeOK ==
    /\ Fault \in Faults
    /\ receipts \in [Slots -> ReceiptType]
    /\ gate_ready \in BOOLEAN

ShadowReceiptPolicy ==
    receipts[ShadowSlot] # EmptyReceipt =>
        /\ receipts[ShadowSlot].kind = "change_certification"
        /\ receipts[ShadowSlot].id = "shared-id"
        /\ receipts[ShadowSlot].assurance = "empirical"
        /\ receipts[ShadowSlot].claim_outcome = "unknown"
        /\ receipts[ShadowSlot].source_proof = FALSE
        /\ receipts[ShadowSlot].approval_authority = FALSE

ReceiptNamespaceSeparated ==
    \A slot \in Slots : receipts[slot] # EmptyReceipt =>
        /\ receipts[slot].kind = SlotKind(slot)
        /\ receipts[slot].id = SlotId(slot)

GateMatchesRequiredSubjects == gate_ready = RequiredGate

====
