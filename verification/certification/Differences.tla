---- MODULE Differences ----
EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS Scenario, Mutant, SourceProof, ApprovalAuthority

Inputs ==
    CASE Scenario = "complete" -> {1, 2}
      [] Scenario = "bounded" -> {1, 2}
      [] Scenario = "violation_missing" -> {1, 2}
      [] Scenario = "empty" -> {}
      [] OTHER -> {1}

InputOrder ==
    CASE Inputs = {} -> <<>>
      [] Inputs = {1, 2} -> <<1, 2>>
      [] OTHER -> <<1>>

OutputModel == {0, 1, 2, 3}
Repetitions == 2
Standing == [i \in {1, 2} |-> IF i = 1 THEN {1, 2} ELSE {2}]

GuardRows ==
    IF Scenario \in {"guard_overlap", "guard_duplicate"} THEN
        { [input |-> 1, outputs |-> {1, 2}],
          [input |-> 1, outputs |-> {2}] }
    ELSE {}

Record(i, isSelected, failed, baseValues, candidateValues) ==
    [input |-> i, selected |-> isSelected, failure |-> failed,
     base |-> baseValues, candidate |-> candidateValues]

Equal1 == Record(1, TRUE, FALSE, <<1, 1>>, <<1, 1>>)
Equal2 == Record(2, TRUE, FALSE, <<2, 2>>, <<2, 2>>)
ShrinkEqual1 == Record(1, FALSE, FALSE, <<1, 1>>, <<1, 1>>)
ShrinkUnstable1 == Record(1, FALSE, FALSE, <<1, 1>>, <<1, 2>>)
ShrinkUnstable2 == Record(2, FALSE, FALSE, <<2, 2>>, <<2, 1>>)
Violation1 == Record(1, TRUE, FALSE, <<1, 1>>, <<2, 2>>)
GuardAllowed1 == Record(1, TRUE, FALSE, <<1, 1>>, <<2, 2>>)
GuardRejected1 == Record(1, TRUE, FALSE, <<1, 1>>, <<1, 1>>)
StandingViolation1 == Record(1, TRUE, FALSE, <<3, 3>>, <<3, 3>>)
UnguardedChange1 == Record(1, TRUE, FALSE, <<1, 1>>, <<2, 2>>)
Repair1 == Record(1, TRUE, FALSE, <<0, 0>>, <<1, 1>>)
BadCandidate1 == Record(1, TRUE, FALSE, <<0, 0>>, <<0, 0>>)
SelectedEqual1 == Record(1, TRUE, FALSE, <<1, 1>>, <<1, 1>>)
SelectedEqual2 == Record(2, TRUE, FALSE, <<2, 2>>, <<2, 2>>)
SelectedChange1 == Record(1, TRUE, FALSE, <<1, 1>>, <<2, 2>>)
SelectedStandingBad1 == Record(1, TRUE, FALSE, <<1, 1>>, <<3, 3>>)
SelectedRepair1 == Record(1, TRUE, FALSE, <<0, 0>>, <<1, 1>>)
SelectedFailed1 == Record(1, TRUE, TRUE, <<1>>, <<1>>)
SelectedUnstable1 == Record(1, TRUE, FALSE, <<1, 2>>, <<1, 1>>)
SelectedPartial1 == Record(1, TRUE, FALSE, <<1>>, <<1>>)
SelectedOutOfModel1 == Record(1, TRUE, FALSE, <<1, 1>>, <<4, 4>>)
ShrinkEqual2 == Record(2, FALSE, FALSE, <<2, 2>>, <<2, 2>>)
SelectedStandingBad2 == Record(2, TRUE, FALSE, <<2, 2>>, <<1, 1>>)

Samples ==
    CASE Scenario = "complete" -> <<Equal1, Equal2>>
      [] Scenario = "reach_conformance" -> <<Equal1>>
      [] Scenario = "bounded" ->
          <<SelectedEqual1, SelectedEqual2, SelectedChange1, SelectedStandingBad1,
            SelectedRepair1, SelectedFailed1, SelectedUnstable1, SelectedPartial1,
            SelectedOutOfModel1, ShrinkEqual1, ShrinkEqual2, ShrinkUnstable1,
            SelectedStandingBad2>>
      [] Scenario = "selected_vs_shrink" -> <<ShrinkEqual1>>
      [] Scenario = "guard_duplicate" -> <<GuardAllowed1>>
      [] Scenario = "invalid_shrink" -> <<Equal1, ShrinkUnstable1>>
      [] Scenario = "violation_missing" -> <<Violation1, ShrinkUnstable2>>
      [] Scenario = "guard_overlap" -> <<GuardAllowed1, GuardRejected1>>
      [] Scenario = "standing_violation" -> <<StandingViolation1>>
      [] Scenario = "unguarded_change" -> <<UnguardedChange1>>
      [] Scenario = "repair" -> <<Repair1>>
      [] Scenario = "bad_candidate" -> <<BadCandidate1>>
      [] OTHER -> <<>>

VARIABLES phase, records, pending, assessed, missing, violations, unknowns,
          repairs, status

vars == <<phase, records, pending, assessed, missing, violations, unknowns,
          repairs, status>>

Init ==
    /\ phase = "collect"
    /\ records = <<>>
    /\ pending = InputOrder
    /\ assessed = <<>>
    /\ missing = <<>>
    /\ violations = <<>>
    /\ unknowns = <<>>
    /\ repairs = <<>>
    /\ status = "unset"

SamplesSet == {Samples[j] : j \in 1..Len(Samples)}
RecordsSet == {records[j] : j \in 1..Len(records)}
PendingInputs == {pending[j] : j \in 1..Len(pending)}
MissingInputs == {missing[j] : j \in 1..Len(missing)}

RecordAt(j) == IF j \in 1..Len(records) THEN records[j] ELSE Record(0, FALSE, TRUE, <<>>, <<>>)
KeepRecord(r, condition) == IF condition THEN <<r>> ELSE <<>>

Stable(values) ==
    \A j, k \in 1..Len(values) : values[j] = values[k]

Valid(r) ==
    /\ ~r.failure
    /\ Len(r.base) = Repetitions
    /\ Len(r.candidate) = Repetitions
    /\ Stable(r.base)
    /\ Stable(r.candidate)
    /\ \A j \in 1..Len(r.base) : r.base[j] \in OutputModel
    /\ \A j \in 1..Len(r.candidate) : r.candidate[j] \in OutputModel

Rows(i) == {r \in RecordsSet : r.input = i}
SelectedRows(i) ==
    KeepRecord(RecordAt(1), Len(records) >= 1 /\ RecordAt(1).input = i /\
               RecordAt(1).selected)
    \o KeepRecord(RecordAt(2), Len(records) >= 2 /\ RecordAt(2).input = i /\
                   RecordAt(2).selected)
BadRowsForInput(i) ==
    KeepRecord(RecordAt(1), Len(records) >= 1 /\ RecordAt(1).input = i /\ ~Valid(RecordAt(1))
        /\ (Mutant # "ignore_shrink_unknown" \/ RecordAt(1).selected))
    \o KeepRecord(RecordAt(2), Len(records) >= 2 /\ RecordAt(2).input = i /\ ~Valid(RecordAt(2))
        /\ (Mutant # "ignore_shrink_unknown" \/ RecordAt(2).selected))

Guarded(i) == \E g \in GuardRows : g.input = i
GuardAllows(i, output) ==
    \A g \in GuardRows : g.input # i \/ output \in g.outputs
GuardUnionAllows(i, output) ==
    \E g \in GuardRows : g.input = i /\ output \in g.outputs

ExpectedClass(r) ==
    IF r.candidate[1] \notin Standing[r.input] THEN "violation"
    ELSE IF Guarded(r.input) THEN
        IF GuardAllows(r.input, r.candidate[1]) THEN "accepted" ELSE "violation"
    ELSE IF r.base[1] = r.candidate[1] THEN "accepted"
    ELSE IF r.base[1] \notin Standing[r.input] THEN "repair"
    ELSE "violation"

ActualClass(r) ==
    IF Mutant = "skip_standing"
       THEN IF Guarded(r.input)
               THEN IF GuardAllows(r.input, r.candidate[1]) THEN "accepted" ELSE "violation"
               ELSE IF r.base[1] = r.candidate[1] THEN "accepted" ELSE "violation"
    ELSE IF Mutant = "union_guards" /\ Guarded(r.input)
       THEN IF GuardUnionAllows(r.input, r.candidate[1]) THEN "accepted" ELSE "violation"
    ELSE IF Mutant = "allow_unguarded_change" /\ ~Guarded(r.input)
       THEN "accepted"
    ELSE IF Mutant = "reject_repair" /\ r.base[1] \notin Standing[r.input]
                               /\ r.candidate[1] \in Standing[r.input]
       THEN "violation"
    ELSE ExpectedClass(r)

CoverageOK(i) ==
    /\ Len(SelectedRows(i)) = 1
    /\ \A j \in 1..Len(SelectedRows(i)) : Valid(SelectedRows(i)[j])

CoverageByRule(i) ==
    IF Mutant = "shrink_fills_coverage"
       THEN \E r \in Rows(i) : Valid(r)
       ELSE CoverageOK(i)

RawViolation(r) ==
    Valid(r) /\
        ((r.candidate[1] \notin Standing[r.input]) \/
         (Guarded(r.input) /\ ~GuardAllows(r.input, r.candidate[1])) \/
         (~Guarded(r.input) /\ r.base[1] \in Standing[r.input] /\
             r.base[1] # r.candidate[1]))

NoDuplicates(sequence) ==
    \A j, k \in 1..Len(sequence) : j < k => sequence[j] # sequence[k]

TypeOK ==
    /\ phase \in {"collect", "assess", "done"}
    /\ status \in {"unset", "conformance", "observed_violation", "unknown"}
    /\ Len(records) <= 2
    /\ \A j \in 1..Len(records) : records[j] \in SamplesSet
    /\ \A sequence \in {pending, assessed, missing} :
           NoDuplicates(sequence) /\ \A j \in 1..Len(sequence) : sequence[j] \in Inputs
    /\ \A sequence \in {violations, unknowns, repairs} :
           \A j \in 1..Len(sequence) : sequence[j] \in RecordsSet

AddRecord(r) ==
    /\ phase = "collect"
    /\ r \in SamplesSet
    /\ Len(records) < 2
    /\ records' = Append(records, r)
    /\ UNCHANGED <<phase, pending, assessed, missing, violations, unknowns,
                    repairs, status>>

RemoveInput(sequence, input) ==
    IF sequence[1] = input THEN
        IF Len(sequence) = 1 THEN <<>> ELSE <<sequence[2]>>
    ELSE <<sequence[1]>>

CloseCollection ==
    /\ phase = "collect"
    /\ phase' = "assess"
    /\ UNCHANGED <<records, pending, assessed, missing, violations, unknowns,
                    repairs, status>>

AssessInput(i) ==
    /\ phase = "assess"
    /\ i \in PendingInputs
    /\ pending' = RemoveInput(pending, i)
    /\ assessed' = Append(assessed, i)
    /\ missing' = IF CoverageByRule(i) THEN missing ELSE Append(missing, i)
    /\ unknowns' = unknowns \o BadRowsForInput(i)
    /\ violations' = violations \o
        KeepRecord(RecordAt(1), Len(records) >= 1 /\ RecordAt(1).input = i /\
                   Valid(RecordAt(1)) /\ ActualClass(RecordAt(1)) = "violation")
        \o KeepRecord(RecordAt(2), Len(records) >= 2 /\ RecordAt(2).input = i /\
                   Valid(RecordAt(2)) /\ ActualClass(RecordAt(2)) = "violation")
    /\ repairs' = repairs \o
        KeepRecord(RecordAt(1), Len(records) >= 1 /\ RecordAt(1).input = i /\
                   Valid(RecordAt(1)) /\ ActualClass(RecordAt(1)) = "repair")
        \o KeepRecord(RecordAt(2), Len(records) >= 2 /\ RecordAt(2).input = i /\
                   Valid(RecordAt(2)) /\ ActualClass(RecordAt(2)) = "repair")
    /\ UNCHANGED <<phase, records, status>>

Finish ==
    /\ phase = "assess"
    /\ pending = <<>>
    /\ phase' = "done"
    /\ status' =
        IF Mutant = "hide_violation"
           THEN IF Len(missing) > 0 \/ Len(unknowns) > 0 THEN "unknown"
                ELSE IF Len(violations) > 0 THEN "observed_violation"
                     ELSE "conformance"
        ELSE IF Mutant = "empty_vacuous" /\ Inputs = {}
           THEN "conformance"
        ELSE IF Len(violations) > 0 THEN "observed_violation"
        ELSE IF Len(missing) = 0 /\ Len(unknowns) = 0 /\ Inputs # {}
           THEN "conformance"
        ELSE "unknown"
    /\ UNCHANGED <<records, pending, assessed, missing, violations, unknowns,
                    repairs>>

Done == phase = "done" /\ UNCHANGED vars
Next == (\E r \in SamplesSet : AddRecord(r))
        \/ CloseCollection
        \/ (\E i \in Inputs : AssessInput(i))
        \/ Finish
        \/ Done
Spec == Init /\ [][Next]_vars

SelectedCoverage ==
    status = "conformance" =>
        /\ Inputs # {}
        /\ \A i \in Inputs : CoverageOK(i)

UnknownBlocksConformance ==
    status = "conformance" =>
        /\ Len(unknowns) = 0
        /\ \A j \in 1..Len(records) : Valid(records[j])

StandingIntent ==
    phase = "done" =>
        \A r \in RecordsSet : Valid(r) /\ r.candidate[1] \notin Standing[r.input]
                                => \E j \in 1..Len(violations) : violations[j] = r

GuardIntersection ==
    phase = "done" =>
        \A r \in RecordsSet :
            Valid(r) /\ Guarded(r.input) /\
            ~GuardAllows(r.input, r.candidate[1]) =>
                \E j \in 1..Len(violations) : violations[j] = r

UnguardedRelation ==
    phase = "done" =>
        \A r \in RecordsSet :
            Valid(r) /\ ~Guarded(r.input) /\
            r.base[1] \in Standing[r.input] /\
            r.base[1] # r.candidate[1] =>
                \E j \in 1..Len(violations) : violations[j] = r

ViolationVisibility ==
    (phase = "done" /\ \E r \in RecordsSet : RawViolation(r)) =>
        status = "observed_violation"

ConformanceSoundness ==
    status = "conformance" =>
        /\ Len(missing) = 0
        /\ Len(violations) = 0
        /\ Len(unknowns) = 0
        /\ \A i \in Inputs : CoverageOK(i)

DuplicateSelectedUnknown ==
    phase = "done" =>
        \A i \in Inputs : Len(SelectedRows(i)) > 1 =>
            i \in MissingInputs /\ status # "conformance"

RepairAccepted ==
    phase = "done" /\ Scenario = "repair" /\ Len(missing) = 0 =>
        /\ status = "conformance"
        /\ \E j \in 1..Len(repairs) : repairs[j] = Repair1

RepairClassification ==
    phase = "done" =>
        \A r \in RecordsSet :
            Valid(r) /\ ExpectedClass(r) = "repair" =>
                /\ \E j \in 1..Len(repairs) : repairs[j] = r
                /\ \A j \in 1..Len(violations) : violations[j] # r

EmptyDomainUnknown ==
    phase = "done" /\ Inputs = {} => status = "unknown"

NoCompletedState == Scenario = "reach_empty_done" => phase # "done"
NoConformance == Scenario = "reach_conformance" => status # "conformance"

AuthorityBoundary == ~SourceProof /\ ~ApprovalAuthority

====
