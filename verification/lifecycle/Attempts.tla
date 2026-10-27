---- MODULE Attempts ----
EXTENDS Naturals, Sequences

CONSTANTS ExpectedJob, ExpectedSource, ForeignJob, ForeignSource, InjectFalsePass

ObservedEvents == <<"unused">>

States == {"absent", "queued", "running", "done", "interrupted"}
Outcomes == {"none", "PASS", "FAIL", "BLOCKED"}
Identities == {"none", ExpectedJob, ForeignJob}
Sources == {"none", ExpectedSource, ForeignSource}
MaxAttempts == 3

VARIABLES state, attempt, cancelRequested, workerAlive, orphaned, outcome,
          job, source, resultJob, resultSource, step

vars == <<state, attempt, cancelRequested, workerAlive, orphaned, outcome,
         job, source, resultJob, resultSource, step>>

Init ==
    /\ state = "absent"
    /\ attempt = 0
    /\ cancelRequested = FALSE
    /\ workerAlive = FALSE
    /\ orphaned = FALSE
    /\ outcome = "none"
    /\ job = "none"
    /\ source = "none"
    /\ resultJob = "none"
    /\ resultSource = "none"
    /\ step = 0

Submit ==
    /\ state = "absent"
    /\ state' = "queued"
    /\ attempt' = 1
    /\ job' = ExpectedJob
    /\ source' = ExpectedSource
    /\ UNCHANGED <<cancelRequested, workerAlive, orphaned, outcome,
                   resultJob, resultSource>>

Start ==
    /\ state = "queued"
    /\ state' = "running"
    /\ workerAlive' = TRUE
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, outcome, job, source,
                   resultJob, resultSource>>

RequestCancel ==
    /\ state \in {"queued", "running"}
    /\ cancelRequested = FALSE
    /\ cancelRequested' = TRUE
    /\ UNCHANGED <<state, attempt, workerAlive, orphaned, outcome, job,
                   source, resultJob, resultSource>>

Complete(result) ==
    /\ state = "running"
    /\ workerAlive
    /\ result \in {"PASS", "FAIL", "BLOCKED"}
    /\ state' = "done"
    /\ outcome' = result
    /\ resultJob' = job
    /\ resultSource' = source
    /\ workerAlive' = FALSE
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, job, source>>

WrongIdentityPass ==
    /\ InjectFalsePass
    /\ state = "running"
    /\ workerAlive
    /\ state' = "done"
    /\ outcome' = "PASS"
    /\ resultJob' = ForeignJob
    /\ resultSource' = ForeignSource
    /\ workerAlive' = FALSE
    /\ UNCHANGED <<attempt, cancelRequested, orphaned, job, source>>

WorkerLost ==
    /\ state = "running"
    /\ workerAlive
    /\ workerAlive' = FALSE
    /\ orphaned' = TRUE
    /\ UNCHANGED <<state, attempt, cancelRequested, outcome, job, source,
                   resultJob, resultSource>>

Recover ==
    /\ state = "running"
    /\ orphaned
    /\ ~workerAlive
    /\ state' = "interrupted"
    /\ orphaned' = FALSE
    /\ UNCHANGED <<attempt, cancelRequested, workerAlive, outcome, job,
                   source, resultJob, resultSource>>

Retry ==
    /\ state \in {"done", "interrupted"}
    /\ ~workerAlive
    /\ attempt < MaxAttempts
    /\ state' = "queued"
    /\ attempt' = attempt + 1
    /\ cancelRequested' = FALSE
    /\ workerAlive' = FALSE
    /\ orphaned' = FALSE
    /\ outcome' = "none"
    /\ resultJob' = "none"
    /\ resultSource' = "none"
    /\ UNCHANGED <<job, source>>

Actions ==
    \/ Submit
    \/ Start
    \/ RequestCancel
    \/ Complete("PASS")
    \/ Complete("FAIL")
    \/ Complete("BLOCKED")
    \/ WrongIdentityPass
    \/ WorkerLost
    \/ Recover
    \/ Retry

Next == /\ Actions /\ step' = step
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in States
    /\ attempt \in 0..MaxAttempts
    /\ cancelRequested \in BOOLEAN
    /\ workerAlive \in BOOLEAN
    /\ orphaned \in BOOLEAN
    /\ outcome \in Outcomes
    /\ job \in Identities
    /\ source \in Sources
    /\ resultJob \in Identities
    /\ resultSource \in Sources
    /\ step \in 0..Len(ObservedEvents)

NoFalsePass ==
    outcome = "PASS" =>
        /\ state = "done"
        /\ resultJob = job
        /\ resultSource = source
        /\ job = ExpectedJob
        /\ source = ExpectedSource

TerminalHasResult == state = "done" => outcome \in {"PASS", "FAIL", "BLOCKED"}
InterruptedHasNoResult == state = "interrupted" => outcome = "none"
IdentityPinned == state # "absent" => job = ExpectedJob /\ source = ExpectedSource

TraceAction ==
    CASE ObservedEvents[step + 1] = "submit" -> Submit
      [] ObservedEvents[step + 1] = "start" -> Start
      [] ObservedEvents[step + 1] = "cancel" -> RequestCancel
      [] ObservedEvents[step + 1] = "complete_pass" -> Complete("PASS")
      [] ObservedEvents[step + 1] = "complete_blocked" -> Complete("BLOCKED")
      [] ObservedEvents[step + 1] = "complete_fail" -> Complete("FAIL")
      [] ObservedEvents[step + 1] = "wrong_identity_pass" -> WrongIdentityPass
      [] ObservedEvents[step + 1] = "worker_lost" -> WorkerLost
      [] ObservedEvents[step + 1] = "recover" -> Recover
      [] ObservedEvents[step + 1] = "retry" -> Retry
      [] OTHER -> FALSE

TraceNext == /\ step < Len(ObservedEvents) /\ TraceAction /\ step' = step + 1
TraceSpec == Init /\ [][TraceNext]_vars /\ WF_vars(TraceNext)
TraceComplete == <> (step = Len(ObservedEvents))

====
