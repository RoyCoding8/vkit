---- MODULE Authority ----
EXTENDS FiniteSets

CONSTANTS Fault, ComponentId,
          TargetPolicy, CandidatePolicy, TargetPackage, CandidatePackage,
          CheckerA, CheckerB, SourceA, SourceB,
          ProtectedTarget, CandidateTarget

NoId == "none"
Policies == {TargetPolicy, CandidatePolicy}
Packages == {TargetPackage, CandidatePackage}
Checkers == {CheckerA, CheckerB}
Sources == {SourceA, SourceB}
Modes == {"none", "local", "ci", "preflight"}
Phases == {"idle", "submitted", "captured", "sealed", "executed",
           "receipted", "published", "reuse_checked", "reuse_rejected", "preflight_done"}
Results == {"none", "PASS", "FAIL", "BLOCKED"}
Targets == {NoId, ProtectedTarget, CandidateTarget}
Claim(policy, package) ==
    [component |-> ComponentId, policy_sha256 |-> policy,
     package_sha256 |-> package]
NoClaim == [component |-> NoId, policy_sha256 |-> NoId, package_sha256 |-> NoId]
Claims == {NoClaim} \cup {Claim(policy, package) : policy \in Policies, package \in Packages}

EmptyRequest == [claim |-> NoClaim, component_claim |-> NoClaim,
                 policy |-> NoId, package |-> NoId, checker |-> NoId,
                 source |-> NoId, capture |-> NoId, host_target |-> NoId]
EmptyJob == [claim |-> NoClaim, policy |-> NoId, package |-> NoId,
             checker |-> NoId, source |-> NoId, host_target |-> NoId]
EmptyReceipt == [claim |-> NoClaim, policy |-> NoId, package |-> NoId,
                 checker |-> NoId, source |-> NoId, host_target |-> NoId,
                 outcome |-> "none"]

VARIABLES phase, mode, live, request, job, jobExists, outcome,
          receipt, receiptExists, preflightReturned, published, publishedPass
vars == <<phase, mode, live, request, job, jobExists, outcome,
          receipt, receiptExists, preflightReturned, published, publishedPass>>

Init ==
    /\ phase = "idle"
    /\ mode = "none"
    /\ live = [policy |-> TargetPolicy, package |-> TargetPackage,
               checker |-> CheckerA, source |-> SourceA,
               candidate_policy |-> CandidatePolicy,
               candidate_package |-> CandidatePackage]
    /\ request = EmptyRequest
    /\ job = EmptyJob
    /\ jobExists = FALSE
    /\ outcome = "none"
    /\ receipt = EmptyReceipt
    /\ receiptExists = FALSE
    /\ preflightReturned = FALSE
    /\ published = FALSE
    /\ publishedPass = FALSE

SubmitLocal ==
    /\ phase = "idle"
    /\ phase' = "submitted"
    /\ mode' = "local"
    /\ request' = [claim |-> IF Fault = "bad_claim"
                                  THEN Claim(live.candidate_policy, live.candidate_package)
                                  ELSE Claim(live.policy, live.package),
                  component_claim |-> Claim(live.policy, live.package),
                  policy |-> live.policy, package |-> live.package,
                  checker |-> live.checker, source |-> live.source,
                  capture |-> NoId, host_target |-> NoId]
    /\ UNCHANGED <<live, job, jobExists, outcome, receipt, receiptExists,
                   preflightReturned, published, publishedPass>>

SubmitCI ==
    /\ phase = "idle"
    /\ phase' = "submitted"
    /\ mode' = "ci"
    /\ request' = [claim |-> Claim(
                         IF Fault = "candidate_target" THEN live.candidate_policy ELSE TargetPolicy,
                         IF Fault = "candidate_target" THEN live.candidate_package ELSE TargetPackage),
                  component_claim |-> Claim(
                         IF Fault = "candidate_target" THEN live.candidate_policy ELSE TargetPolicy,
                         IF Fault = "candidate_target" THEN live.candidate_package ELSE TargetPackage),
                  policy |-> IF Fault = "candidate_target" THEN live.candidate_policy ELSE TargetPolicy,
                  package |-> IF Fault = "candidate_target" THEN live.candidate_package ELSE TargetPackage,
                  checker |-> live.checker, source |-> live.source,
                  capture |-> NoId,
                  host_target |-> IF Fault = "candidate_target" THEN CandidateTarget ELSE ProtectedTarget]
    /\ UNCHANGED <<live, job, jobExists, outcome, receipt, receiptExists,
                   preflightReturned, published, publishedPass>>

ReturnPreflight ==
    /\ phase = "idle"
    /\ phase' = "preflight_done"
    /\ mode' = "preflight"
    /\ preflightReturned' = TRUE
    /\ UNCHANGED <<live, request, job, jobExists, outcome, receipt,
                   receiptExists, published, publishedPass>>

DriftPolicy ==
    /\ phase \notin {"reuse_checked", "reuse_rejected"}
    /\ live.policy = TargetPolicy
    /\ live.package = TargetPackage
    /\ live' = [live EXCEPT !.policy = CandidatePolicy,
                             !.package = CandidatePackage]
    /\ UNCHANGED <<phase, mode, request, job, jobExists, outcome, receipt,
                   receiptExists, preflightReturned, published, publishedPass>>

DriftChecker ==
    /\ phase \notin {"reuse_checked", "reuse_rejected"}
    /\ live.checker = CheckerA
    /\ live' = [live EXCEPT !.checker = CheckerB]
    /\ UNCHANGED <<phase, mode, request, job, jobExists, outcome, receipt,
                   receiptExists, preflightReturned, published, publishedPass>>

DriftSource ==
    /\ phase \notin {"reuse_checked", "reuse_rejected"}
    /\ live.source = SourceA
    /\ live' = [live EXCEPT !.source = SourceB]
    /\ UNCHANGED <<phase, mode, request, job, jobExists, outcome, receipt,
                   receiptExists, preflightReturned, published, publishedPass>>

CaptureCandidate ==
    /\ phase = "submitted"
    /\ phase' = "captured"
    /\ request' = [request EXCEPT !.capture =
                       IF Fault = "wrong_capture" THEN SourceB ELSE request.source]
    /\ UNCHANGED <<mode, live, job, jobExists, outcome, receipt, receiptExists,
                   preflightReturned, published, publishedPass>>

SealJob ==
    /\ phase = "captured"
    /\ (request.claim = request.component_claim \/ Fault = "bad_claim")
    /\ (request.capture = request.source \/ Fault = "wrong_capture")
    /\ phase' = "sealed"
    /\ jobExists' = TRUE
    /\ job' = [claim |-> request.component_claim,
               policy |-> request.policy, package |-> request.package,
               checker |-> request.checker, source |-> request.capture,
               host_target |-> request.host_target]
    /\ UNCHANGED <<mode, live, request, outcome, receipt, receiptExists,
                   preflightReturned, published, publishedPass>>

Execute ==
    /\ phase = "sealed"
    /\ jobExists
    /\ phase' = "executed"
    /\ outcome' \in {"PASS", "FAIL", "BLOCKED"}
    /\ UNCHANGED <<mode, live, request, job, jobExists, receipt,
                   receiptExists, preflightReturned, published, publishedPass>>

SealReceipt ==
    /\ phase = "executed"
    /\ phase' = "receipted"
    /\ receiptExists' = TRUE
    /\ receipt' = [claim |-> IF Fault = "swapped_receipt"
                                  THEN Claim(live.candidate_policy, live.candidate_package)
                                  ELSE job.claim,
                  policy |-> job.policy, package |-> job.package,
                  checker |-> job.checker, source |-> job.source,
                  host_target |-> job.host_target, outcome |-> outcome]
    /\ UNCHANGED <<mode, live, request, job, jobExists, outcome,
                   preflightReturned, published, publishedPass>>

ReceiptValid ==
    /\ jobExists
    /\ receiptExists
    /\ receipt.claim = job.claim
    /\ receipt.policy = job.policy
    /\ receipt.package = job.package
    /\ receipt.checker = job.checker
    /\ receipt.source = job.source
    /\ receipt.host_target = job.host_target
    /\ receipt.outcome = outcome

Publish ==
    /\ phase = "receipted"
    /\ ReceiptValid
    /\ phase' = "published"
    /\ published' = TRUE
    /\ publishedPass' = (outcome = "PASS")
    /\ UNCHANGED <<mode, live, request, job, jobExists, outcome,
                   receipt, receiptExists, preflightReturned>>

PromotePreflight ==
    /\ Fault = "promote_preflight"
    /\ phase = "preflight_done"
    /\ preflightReturned
    /\ phase' = "published"
    /\ jobExists' = TRUE
    /\ job' = EmptyJob
    /\ outcome' = "PASS"
    /\ receiptExists' = TRUE
    /\ receipt' = [EmptyReceipt EXCEPT !.outcome = "PASS"]
    /\ published' = TRUE
    /\ publishedPass' = TRUE
    /\ UNCHANGED <<mode, live, request, preflightReturned>>

LivePinsMatchJob ==
    /\ live.policy = job.policy
    /\ live.package = job.package
    /\ live.checker = job.checker
    /\ live.source = job.source
    /\ Claim(live.policy, live.package) = job.claim

TryReuse ==
    /\ phase = "published"
    /\ mode = "local"
    /\ ReceiptValid
    /\ phase' = IF Fault = "false_reject" THEN "reuse_rejected" ELSE IF outcome = "PASS" \land (Fault = "stale_reuse" \lor LivePinsMatchJob)
                     THEN "reuse_checked" ELSE "reuse_rejected"
    /\ UNCHANGED <<mode, live, request, job, jobExists, outcome, receipt,
                   receiptExists, preflightReturned, published, publishedPass>>

Next ==
    \/ SubmitLocal
    \/ SubmitCI
    \/ ReturnPreflight
    \/ DriftPolicy
    \/ DriftChecker
    \/ DriftSource
    \/ CaptureCandidate
    \/ SealJob
    \/ Execute
    \/ SealReceipt
    \/ Publish
    \/ TryReuse
    \/ PromotePreflight

Spec == Init /\ [][Next]_vars

RequestType == [claim : Claims, component_claim : Claims,
                policy : Policies \cup {NoId}, package : Packages \cup {NoId},
                checker : Checkers \cup {NoId}, source : Sources \cup {NoId},
                capture : Sources \cup {NoId}, host_target : Targets]
JobType == [claim : Claims, policy : Policies \cup {NoId},
            package : Packages \cup {NoId}, checker : Checkers \cup {NoId},
            source : Sources \cup {NoId}, host_target : Targets]
ReceiptType == [claim : Claims, policy : Policies \cup {NoId},
                package : Packages \cup {NoId}, checker : Checkers \cup {NoId},
                source : Sources \cup {NoId}, host_target : Targets, outcome : Results]

TypeOK ==
    /\ phase \in Phases
    /\ mode \in Modes
    /\ live \in [policy : Policies, package : Packages,
                  checker : Checkers, source : Sources,
                  candidate_policy : Policies, candidate_package : Packages]
    /\ request \in RequestType
    /\ job \in JobType
    /\ jobExists \in BOOLEAN
    /\ outcome \in Results
    /\ receipt \in ReceiptType
    /\ receiptExists \in BOOLEAN
    /\ preflightReturned \in BOOLEAN
    /\ published \in BOOLEAN
    /\ publishedPass \in BOOLEAN

ClaimPinned ==
    mode \in {"local", "ci"} /\ phase # "idle" =>
        /\ request.claim = request.component_claim
        /\ jobExists => job.claim = request.component_claim
        /\ jobExists => job.claim = Claim(job.policy, job.package)

TargetAuthorityPinned ==
    mode = "ci" /\ phase # "idle" =>
        /\ request.host_target = ProtectedTarget
        /\ request.claim = Claim(TargetPolicy, TargetPackage)
        /\ request.policy = TargetPolicy
        /\ request.package = TargetPackage
        /\ (jobExists => job.host_target = ProtectedTarget)

SnapshotPinned ==
    jobExists =>
        /\ job.policy = request.policy
        /\ job.package = request.package
        /\ job.checker = request.checker

CandidateCapturePinned ==
    mode \in {"local", "ci"} /\ phase \in {"captured", "sealed", "executed", "receipted", "published", "reuse_checked", "reuse_rejected"} =>
        /\ request.capture = request.source
        /\ (jobExists => job.source = request.source)

ReceiptBound ==
    receiptExists => ReceiptValid

PublishedPassBound ==
    publishedPass => published /\ ReceiptValid /\ outcome = "PASS"

PreflightNotProof ==
    preflightReturned =>
        /\ mode = "preflight"
        /\ phase = "preflight_done"
        /\ ~jobExists
        /\ ~receiptExists
        /\ outcome = "none"
        /\ ~published
        /\ ~publishedPass

ReuseDecisionBound ==
    phase = "reuse_checked" =>
        /\ mode = "local"
        /\ ReceiptValid
        /\ outcome = "PASS"
        /\ LivePinsMatchJob

ReuseRejectedBound ==
    phase = "reuse_rejected" => outcome # "PASS" \/ ~LivePinsMatchJob

====
