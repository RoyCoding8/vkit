---- MODULE Attempts ----
EXTENDS Naturals, FiniteSets

CONSTANTS ExpectedJob, ExpectedSource, ForeignJob, ForeignSource, InjectFalsePass

AttemptIds == {1, 2, 3}
States == {"absent", "queued", "running", "done", "interrupted"}
Outcomes == {"none", "PASS", "FAIL", "BLOCKED"}
Identities == {"none", ExpectedJob, ForeignJob}
Sources == {"none", ExpectedSource, ForeignSource}
MaxAttempts == 3

VARIABLES state, attempt, cancelRequested, workerAlive, orphaned, outcome,
          job, source, resultJob, resultSource, published

vars == <<state, attempt, cancelRequested, workerAlive, orphaned, outcome,
         job, source, resultJob, resultSource, published>>

Init ==
    /\ state = [i \in AttemptIds |-> "absent"]
    /\ attempt = [i \in AttemptIds |-> 0]
    /\ cancelRequested = [i \in AttemptIds |-> FALSE]
    /\ workerAlive = [i \in AttemptIds |-> FALSE]
    /\ orphaned = [i \in AttemptIds |-> FALSE]
    /\ outcome = [i \in AttemptIds |-> "none"]
    /\ job = [i \in AttemptIds |-> "none"]
    /\ source = [i \in AttemptIds |-> "none"]
    /\ resultJob = [i \in AttemptIds |-> "none"]
    /\ resultSource = [i \in AttemptIds |-> "none"]
    /\ published = 0

Submit(i) ==
    /\ i \in AttemptIds
    /\ state[i] = "absent"
    /\ state' = [state EXCEPT ![i] = "queued"]
    /\ attempt' = [attempt EXCEPT ![i] = 1]
    /\ job' = [job EXCEPT ![i] = ExpectedJob]
    /\ source' = [source EXCEPT ![i] = ExpectedSource]
    /\ UNCHANGED <<cancelRequested, workerAlive, orphaned, outcome,
                   resultJob, resultSource, published>>

Start(i) ==
    /\ i \in AttemptIds
    /\ state[i] = "queued"
    /\ state' = [state EXCEPT ![i] = "running"]
    /\ workerAlive' = [workerAlive EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, outcome, job, source,
                   resultJob, resultSource, published>>

RequestCancel(i) ==
    /\ i \in AttemptIds
    /\ state[i] \in {"queued", "running"}
    /\ cancelRequested[i] = FALSE
    /\ cancelRequested' = [cancelRequested EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<state, attempt, workerAlive, orphaned, outcome, job,
                   source, resultJob, resultSource, published>>

Complete(i, result) ==
    /\ i \in AttemptIds
    /\ state[i] = "running"
    /\ workerAlive[i]
    /\ result \in {"PASS", "FAIL", "BLOCKED"}
    /\ state' = [state EXCEPT ![i] = "done"]
    /\ outcome' = [outcome EXCEPT ![i] = result]
    /\ resultJob' = [resultJob EXCEPT ![i] = job[i]]
    /\ resultSource' = [resultSource EXCEPT ![i] = source[i]]
    /\ workerAlive' = [workerAlive EXCEPT ![i] = FALSE]
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, job, source, published>>

WrongIdentityPass(i) ==
    /\ InjectFalsePass
    /\ i \in AttemptIds
    /\ state[i] = "running"
    /\ workerAlive[i]
    /\ state' = [state EXCEPT ![i] = "done"]
    /\ outcome' = [outcome EXCEPT ![i] = "PASS"]
    /\ resultJob' = [resultJob EXCEPT ![i] = ForeignJob]
    /\ resultSource' = [resultSource EXCEPT ![i] = ForeignSource]
    /\ workerAlive' = [workerAlive EXCEPT ![i] = FALSE]
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, job, source, published>>

WorkerLost(i) ==
    /\ i \in AttemptIds
    /\ state[i] = "running"
    /\ workerAlive[i]
    /\ workerAlive' = [workerAlive EXCEPT ![i] = FALSE]
    /\ orphaned' = [orphaned EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<state, attempt, cancelRequested, outcome, job, source,
                   resultJob, resultSource, published>>

Recover(i) ==
    /\ i \in AttemptIds
    /\ state[i] = "running"
    /\ orphaned[i]
    /\ ~workerAlive[i]
    /\ state' = [state EXCEPT ![i] = "interrupted"]
    /\ orphaned' = [orphaned EXCEPT ![i] = FALSE]
    /\ UNCHANGED <<attempt, cancelRequested, workerAlive, outcome, job,
                   source, resultJob, resultSource, published>>

Retry(from, to) ==
    /\ from \in AttemptIds
    /\ to \in AttemptIds
    /\ from # to
    /\ state[from] \in {"done", "interrupted"}
    /\ ~workerAlive[from]
    /\ state[to] = "absent"
    /\ attempt[from] < MaxAttempts
    /\ state' = [state EXCEPT ![to] = "queued"]
    /\ attempt' = [attempt EXCEPT ![to] = attempt[from] + 1]
    /\ cancelRequested' = [cancelRequested EXCEPT ![to] = FALSE]
    /\ workerAlive' = [workerAlive EXCEPT ![to] = FALSE]
    /\ orphaned' = [orphaned EXCEPT ![to] = FALSE]
    /\ outcome' = [outcome EXCEPT ![to] = "none"]
    /\ job' = [job EXCEPT ![to] = job[from]]
    /\ source' = [source EXCEPT ![to] = source[from]]
    /\ resultJob' = [resultJob EXCEPT ![to] = "none"]
    /\ resultSource' = [resultSource EXCEPT ![to] = "none"]
    /\ UNCHANGED <<published>>

Publish(i) ==
    /\ i \in AttemptIds
    /\ state[i] = "done"
    /\ outcome[i] \in {"PASS", "FAIL", "BLOCKED"}
    /\ published' = i
    /\ UNCHANGED <<state, attempt, cancelRequested, workerAlive, orphaned,
                   outcome, job, source, resultJob, resultSource>>

Actions ==
    \/ \E i \in AttemptIds : Submit(i)
    \/ \E i \in AttemptIds : Start(i)
    \/ \E i \in AttemptIds : RequestCancel(i)
    \/ \E i \in AttemptIds : Complete(i, "PASS")
    \/ \E i \in AttemptIds : Complete(i, "FAIL")
    \/ \E i \in AttemptIds : Complete(i, "BLOCKED")
    \/ \E i \in AttemptIds : WrongIdentityPass(i)
    \/ \E i \in AttemptIds : WorkerLost(i)
    \/ \E i \in AttemptIds : Recover(i)
    \/ \E i, j \in AttemptIds : Retry(i, j)
    \/ \E i \in AttemptIds : Publish(i)

Next == Actions
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in [AttemptIds -> States]
    /\ attempt \in [AttemptIds -> 0..MaxAttempts]
    /\ cancelRequested \in [AttemptIds -> BOOLEAN]
    /\ workerAlive \in [AttemptIds -> BOOLEAN]
    /\ orphaned \in [AttemptIds -> BOOLEAN]
    /\ outcome \in [AttemptIds -> Outcomes]
    /\ job \in [AttemptIds -> Identities]
    /\ source \in [AttemptIds -> Sources]
    /\ resultJob \in [AttemptIds -> Identities]
    /\ resultSource \in [AttemptIds -> Sources]
    /\ published \in 0..3

NoFalsePass ==
    \A i \in AttemptIds : outcome[i] = "PASS" =>
        /\ state[i] = "done"
        /\ resultJob[i] = job[i]
        /\ resultSource[i] = source[i]
        /\ job[i] = ExpectedJob
        /\ source[i] = ExpectedSource

TerminalHasResult ==
    \A i \in AttemptIds : state[i] = "done" =>
        outcome[i] \in {"PASS", "FAIL", "BLOCKED"}

InterruptedHasNoResult ==
    \A i \in AttemptIds : state[i] = "interrupted" => outcome[i] = "none"

IdentityPinned ==
    \A i \in AttemptIds : state[i] # "absent" =>
        /\ job[i] = ExpectedJob
        /\ source[i] = ExpectedSource

PublishedAttemptComplete ==
    published > 0 =>
        /\ state[published] = "done"
        /\ outcome[published] \in {"PASS", "FAIL", "BLOCKED"}
        /\ job[published] = ExpectedJob
        /\ source[published] = ExpectedSource

====
