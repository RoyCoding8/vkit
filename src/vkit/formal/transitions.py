from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import product
from typing import Any

from ..inputs import canonical_digest
from ..schemas import validate

Context = tuple[str, str, str]
Edge = tuple[str, str, str, str]
MAX_EXPANDED_EDGES = 65536


@dataclass(frozen=True, order=True)
class TransitionRule:
    source: str
    action: str
    inputs: tuple[str, ...]
    targets: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "action": self.action,
                "inputs": list(self.inputs), "targets": list(self.targets)}


@dataclass(frozen=True)
class FiniteTransitionModel:
    states: tuple[str, ...]
    actions: tuple[str, ...]
    inputs: tuple[str, ...]
    observations: tuple[str, ...]
    initial_states: tuple[str, ...]
    assumptions: frozenset[Context]
    rules: tuple[TransitionRule, ...]
    state_observations: tuple[tuple[str, str], ...]

    @classmethod
    def from_validated_json(cls, raw: dict[str, Any]) -> FiniteTransitionModel:
        states, actions, inputs, observations, initial = (
            tuple(sorted(raw[key]))
            for key in ("states", "actions", "inputs", "observations", "initial_states")
        )
        if not set(initial) <= set(states):
            raise ValueError("initial states must belong to the declared states")
        assumptions = frozenset((row["state"], row["action"], row["input"])
                                for row in raw["assumptions"])
        if not assumptions <= set(product(states, actions, inputs)):
            raise ValueError("assumptions must use declared state, action and input tokens")
        rules = tuple(sorted(TransitionRule(row["source"], row["action"],
                                            tuple(sorted(row["inputs"])), tuple(sorted(row["targets"])))
                             for row in raw["rules"]))
        if len(set(rules)) != len(rules):
            raise ValueError("duplicate normalized transition rule")
        for rule in rules:
            if (rule.source not in states or rule.action not in actions
                    or not set(rule.inputs) <= set(inputs) or not set(rule.targets) <= set(states)):
                raise ValueError("transition rules must use declared tokens")
        state_observations = tuple(sorted((row["state"], row["value"])
                                          for row in raw["state_observations"]))
        if (tuple(state for state, _ in state_observations) != states
                or any(value not in observations for _, value in state_observations)):
            raise ValueError("state observations must assign one declared value to every state")
        return cls(states, actions, inputs, observations, initial, assumptions, rules, state_observations)

    def to_json(self) -> dict[str, Any]:
        return {
            "states": list(self.states), "actions": list(self.actions),
            "inputs": list(self.inputs), "observations": list(self.observations),
            "initial_states": list(self.initial_states),
            "assumptions": [{"state": state, "action": action, "input": value}
                            for state, action, value in sorted(self.assumptions)],
            "rules": [rule.to_json() for rule in self.rules],
            "state_observations": [{"state": state, "value": value}
                                   for state, value in self.state_observations],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_json())

    @property
    def context_count(self) -> int:
        return len(self.states) * len(self.actions) * len(self.inputs)

    @property
    def expanded_edge_count(self) -> int:
        return sum(len(rule.inputs) * len(rule.targets) for rule in self.rules)


@dataclass(frozen=True)
class ReachableModel:
    paths: dict[str, tuple[Edge, ...]]
    edges: frozenset[Edge]
    assumptions: frozenset[Context]
    enabled_contexts: frozenset[Context]
    observations: frozenset[tuple[str, str]]


def _reachable(model: FiniteTransitionModel) -> ReachableModel:
    edges = frozenset((rule.source, rule.action, value, target)
                      for rule in model.rules for value in rule.inputs for target in rule.targets
                      if (rule.source, rule.action, value) in model.assumptions)
    outgoing: dict[str, list[Edge]] = {}
    for edge in sorted(edges):
        outgoing.setdefault(edge[0], []).append(edge)
    paths: dict[str, tuple[Edge, ...]] = {state: () for state in model.initial_states}
    queue = deque(model.initial_states)
    while queue:
        source = queue.popleft()
        for edge in outgoing.get(source, ()):
            target = edge[3]
            if target not in paths:
                paths[target] = (*paths[source], edge)
                queue.append(target)
    reachable_edges = frozenset(edge for edge in edges if edge[0] in paths)
    return ReachableModel(
        paths, reachable_edges,
        frozenset(context for context in model.assumptions if context[0] in paths),
        frozenset(edge[:3] for edge in reachable_edges),
        frozenset(row for row in model.state_observations if row[0] in paths),
    )


def _witness(side: str, value: tuple[str, ...], paths: dict[str, tuple[Edge, ...]],
             *, transition: bool = False) -> dict[str, Any]:
    path = paths[value[0]]
    if transition:
        path = (*path, value)
    start = path[0][0] if path else value[0]
    result: dict[str, Any] = {
        "side": side, "initial_state": start,
        "path": [{"source": source, "action": action, "input": item, "target": target}
                 for source, action, item, target in path],
        "difference": list(value),
    }
    if len(value) == 3:
        result["probe"] = {"state": value[0], "action": value[1], "input": value[2]}
    return result


def compare_transition_models(reference: FiniteTransitionModel, candidate: FiniteTransitionModel,
                              *, max_contexts: int = 32768) -> dict[str, Any]:
    counts = {"reference": {"declared_states": len(reference.states),
                             "declared_contexts": reference.context_count,
                             "expanded_rule_edges": reference.expanded_edge_count},
              "candidate": {"declared_states": len(candidate.states),
                             "declared_contexts": candidate.context_count,
                             "expanded_rule_edges": candidate.expanded_edge_count}}
    report: dict[str, Any] = {
        "schema_version": 1, "scope": "explicit_finite_lts_v1",
        "semantics": "exact_named_state_reachable_behavior",
        "reference_sha256": reference.digest, "candidate_sha256": candidate.digest,
        "complete": False, "counts": counts, "deltas": [],
        "source_proof": False, "approval_authority": False,
    }
    different_domains = [name for name in ("states", "actions", "inputs", "observations")
                         if getattr(reference, name) != getattr(candidate, name)]
    if different_domains:
        return {**report, "kind": "incomparable", "reason": "different_semantic_domains",
                "different_domains": different_domains}
    if reference.context_count + candidate.context_count > max_contexts:
        return {**report, "kind": "unknown", "reason": "context_budget_exceeded"}
    if reference.expanded_edge_count + candidate.expanded_edge_count > MAX_EXPANDED_EDGES:
        return {**report, "kind": "unknown", "reason": "expanded_edge_budget_exceeded"}
    before, after = _reachable(reference), _reachable(candidate)
    for name, model in (("reference", before), ("candidate", after)):
        counts[name].update(reachable_states=len(model.paths), effective_edges=len(model.edges),
                            examined_contexts=reference.context_count)
    comparisons = (
        ("initial_states", frozenset((state,) for state in reference.initial_states),
         frozenset((state,) for state in candidate.initial_states)),
        ("reachable_states", frozenset((state,) for state in before.paths),
         frozenset((state,) for state in after.paths)),
        ("assumptions", before.assumptions, after.assumptions),
        ("guards", before.enabled_contexts, after.enabled_contexts),
        ("transitions", before.edges, after.edges),
        ("observations", before.observations, after.observations),
    )
    deltas = []
    for kind, left, right in comparisons:
        if left == right:
            continue
        witnesses = [_witness(side, value, model.paths, transition=kind == "transitions")
                     for side, values, model in (("reference", left - right, before),
                                                 ("candidate", right - left, after))
                     for value in sorted(values)]
        witness = min(witnesses, key=lambda item: (len(item["path"]), item["side"], item["difference"]))
        deltas.append({"kind": kind, "removed": len(left - right), "added": len(right - left),
                       "witness": witness})
    return {**report, "kind": "different" if deltas else "equivalent", "complete": True,
            "deltas": deltas}


def compare_transition_request(raw: dict[str, Any]) -> dict[str, Any]:
    validate("finite transition comparison", "transition-comparison.v1.json", raw)
    reference = FiniteTransitionModel.from_validated_json(raw["reference"])
    candidate = FiniteTransitionModel.from_validated_json(raw["candidate"])
    return compare_transition_models(reference, candidate, max_contexts=raw.get("max_contexts", 32768))
