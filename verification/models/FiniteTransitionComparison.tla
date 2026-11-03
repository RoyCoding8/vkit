---- MODULE FiniteTransitionComparison ----
EXTENDS Naturals, FiniteSets, Sequences, TLC

States == {"s0", "s1"}
Actions == {"step"}
Inputs == {"x"}
Contexts == {<<state, "step", "x">> : state \in States}
Edges == {<<source, "step", "x", target>> : source \in States, target \in States}

VARIABLES refInitial, candInitial, refAssumptions, candAssumptions,
          refRules, candRules

vars == <<refInitial, candInitial, refAssumptions, candAssumptions,
          refRules, candRules>>

Init ==
    /\ refInitial \in SUBSET States \ {{}}
    /\ candInitial \in SUBSET States \ {{}}
    /\ refAssumptions \in SUBSET Contexts
    /\ candAssumptions \in SUBSET Contexts
    /\ refRules \in SUBSET Edges
    /\ candRules \in SUBSET Edges

Effective(rules, assumptions) ==
    {edge \in rules : <<edge[1], edge[2], edge[3]>> \in assumptions}

Step(sources, edges) ==
    {edge[4] : edge \in {item \in edges : item[1] \in sources}}

Reachable(initial, rules, assumptions) ==
    LET edges == Effective(rules, assumptions)
        first == initial \cup Step(initial, edges)
    IN first \cup Step(first, edges)

ReachEdges(states, rules, assumptions) ==
    {edge \in Effective(rules, assumptions) : edge[1] \in states}

ReachAssumptions(states, assumptions) ==
    {context \in assumptions : context[1] \in states}

EnabledContexts(states, edges) ==
    {<<edge[1], edge[2], edge[3]>> :
        edge \in {item \in edges : item[1] \in states}}

Observations(states) ==
    {<<state, IF state = "s0" THEN "zero" ELSE "one">> : state \in states}

RefReachable == Reachable(refInitial, refRules, refAssumptions)
CandReachable == Reachable(candInitial, candRules, candAssumptions)
RefEdges == ReachEdges(RefReachable, refRules, refAssumptions)
CandEdges == ReachEdges(CandReachable, candRules, candAssumptions)
RefEnabled == EnabledContexts(RefReachable, RefEdges)
CandEnabled == EnabledContexts(CandReachable, CandEdges)
RefReachAssumptions == ReachAssumptions(RefReachable, refAssumptions)
CandReachAssumptions == ReachAssumptions(CandReachable, candAssumptions)
RefObservations == Observations(RefReachable)
CandObservations == Observations(CandReachable)

DeltaFlags ==
    << refInitial # candInitial,
       RefReachable # CandReachable,
       RefReachAssumptions # CandReachAssumptions,
       RefEnabled # CandEnabled,
       RefEdges # CandEdges,
       RefObservations # CandObservations >>

DeltaCounts ==
    << <<Cardinality(refInitial \ candInitial), Cardinality(candInitial \ refInitial)>>,
       <<Cardinality(RefReachable \ CandReachable), Cardinality(CandReachable \ RefReachable)>>,
       <<Cardinality(RefReachAssumptions \ CandReachAssumptions),
         Cardinality(CandReachAssumptions \ RefReachAssumptions)>>,
       <<Cardinality(RefEnabled \ CandEnabled), Cardinality(CandEnabled \ RefEnabled)>>,
       <<Cardinality(RefEdges \ CandEdges), Cardinality(CandEdges \ RefEdges)>>,
       <<Cardinality(RefObservations \ CandObservations),
         Cardinality(CandObservations \ RefObservations)>> >>

Kind == IF DeltaFlags = <<FALSE, FALSE, FALSE, FALSE, FALSE, FALSE>>
        THEN "equivalent" ELSE "different"

Bit(value, weight) == IF value THEN weight ELSE 0
InitialMask(initial) == Bit("s0" \in initial, 1) + Bit("s1" \in initial, 2)
AssumptionMask(assumptions) ==
    Bit(<<"s0", "step", "x">> \in assumptions, 1)
      + Bit(<<"s1", "step", "x">> \in assumptions, 2)
EdgeMask(edges) ==
    Bit(<<"s0", "step", "x", "s0">> \in edges, 1)
      + Bit(<<"s0", "step", "x", "s1">> \in edges, 2)
      + Bit(<<"s1", "step", "x", "s0">> \in edges, 4)
      + Bit(<<"s1", "step", "x", "s1">> \in edges, 8)
ModelId(initial, assumptions, rules) ==
    EdgeMask(rules) + 16 * AssumptionMask(assumptions) +
        64 * (InitialMask(initial) - 1)

TypeOK ==
    /\ refInitial \in SUBSET States \ {{}}
    /\ candInitial \in SUBSET States \ {{}}
    /\ refAssumptions \in SUBSET Contexts
    /\ candAssumptions \in SUBSET Contexts
    /\ refRules \in SUBSET Edges
    /\ candRules \in SUBSET Edges
    /\ RefReachable \subseteq States
    /\ CandReachable \subseteq States
    /\ RefEdges \subseteq Edges
    /\ CandEdges \subseteq Edges
    /\ RefEnabled \subseteq Contexts
    /\ CandEnabled \subseteq Contexts

BridgeOutput == PrintT(<<"VKIT_FINITE_TRANSITION_PAIR_V1",
    ModelId(refInitial, refAssumptions, refRules),
    ModelId(candInitial, candAssumptions, candRules),
    Kind, DeltaFlags, DeltaCounts,
    Cardinality(RefReachable), Cardinality(CandReachable),
    Cardinality(RefEdges), Cardinality(CandEdges)>>)

Next == UNCHANGED vars
Spec == Init /\ [][Next]_vars

====
