---- MODULE DifferenceDecisionBridge ----
EXTENDS Differences, TLC

WireRecord(r) == <<r.input, r.selected, r.failure, r.base, r.candidate>>
WireRecords(sequence) == [j \in 1..Len(sequence) |-> WireRecord(sequence[j])]
CoverageComplete == Inputs # {} /\ Len(missing) = 0

BridgeOutput ==
    phase = "done" =>
        PrintT(<<"VKIT_DIFF_DECISION_V1", Scenario, WireRecords(records), status,
               CoverageComplete, missing, WireRecords(violations),
               WireRecords(unknowns), WireRecords(repairs)>>)

====
