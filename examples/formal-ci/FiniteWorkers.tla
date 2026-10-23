---- MODULE FiniteWorkers ----
CONSTANT Workers
VARIABLE worker

Init == worker = 1
Next == worker' \in Workers
Spec == Init /\ [][Next]_worker

TypeOK == worker \in Workers
OnlyOne == worker = 1
====
