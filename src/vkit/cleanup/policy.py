"""Cleanup policy: modes, rule ids, exclusions, and the owner enabling them once.

Automatic cleanup is enabled by an owner, through approved project policy, one
time. This module is that policy expressed as data plus the one function that
reads it, and it owns the decision of whether a write may happen at all.

## Why the modes are a sum and not a flag

`off`, `preview` and `apply_verified` are three different answers to "may this
tool write?", and collapsing them into a boolean is how a preview tool ends up
writing. The plan asks for explicit modes, so they are an enum with no default:
a caller supplies one, and a missing mode is a refusal rather than the most
permissive member of the set. `APPLY_MODES` is the closed set a caller may
choose from, so "did you mean one of these" is answerable from the refusal
instead of from a guess.

Enabling `apply_verified` authorizes exactly the rules and paths the policy
names. It does not authorize stripping every comment in the repository, and
`rules_for` is where that bound is drawn: a mode is necessary and never
sufficient, and every apply checks both.

## Exclusions are matched against the repository-relative path

An exclusion is a POSIX-style relative path prefix with glob wildcards. It is
matched against the repository-relative path, never against the absolute path,
so a policy that excludes `vendor/*` means the same thing on Windows, in a
worktree, and in CI. An exclusion is a prefix match on whole path segments, so
`build` does not exclude `build_helper.py` -- a rule that excluded more than it
said would be a rule nobody could reason about.

## The identity of the policy

`policy_digest` is a sha256 over the canonical form of the policy itself, so a
receipt can name the policy that authorized a write and a later reader can tell
whether that policy is still the one in force. That is what binds a proposal to
"the approved cleanup policy" rather than to a filename.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .comments import ECMASCRIPT_SUFFIXES, TRAILING_RULE
from .logic import REGISTERED_RULES, REQUIRED_OPTIMIZE_LEVELS, TESTED_CPYTHON_VERSIONS

#: The rule id an applied comment proposal is recorded under. Named here because
#: a policy has to be able to enable it, and a policy that could only enable the
#: two logic rules would refuse a comment edit while appearing to authorize it.
TRAILING_COMMENT_RULE = TRAILING_RULE

#: Every rule id a policy may enable: the two logic rules plus the one comment
#: rule whose proposal this package can write. Checked at construction, so a
#: policy naming anything else is refused rather than reading as coverage the
#: tool does not have.
CLEANUP_RULE_IDS: tuple[str, ...] = (*REGISTERED_RULES, TRAILING_COMMENT_RULE)


class CleanupMode(str, Enum):
    """What the owner has authorized. There is no default member.

    A caller that supplies no mode gets a refusal, not `preview` and certainly
    not `apply_verified`. The permissive reading of an absent mode is the one
    that writes to someone's repository without being asked.
    """

    OFF = "off"
    PREVIEW = "preview"
    APPLY_VERIFIED = "apply_verified"


#: The closed set a caller may choose from, so a refusal can quote it.
CLEANUP_MODES: tuple[str, ...] = tuple(mode.value for mode in CleanupMode)

#: The modes under which a write may happen. `preview` is deliberately absent:
#: it proposes and never writes.
APPLY_MODES: frozenset[str] = frozenset({CleanupMode.APPLY_VERIFIED.value})


@dataclass(frozen=True)
class CleanupPolicy:
    """The owner's approved cleanup policy, and the only thing that can enable a write.

    A policy is data. Nothing in it can name a filesystem path to transform, a
    function to call, or a script to run -- the surface is a mode, a set of
    registered rule ids, and a set of path patterns. That is the plan's "do not
    accept arbitrary transformation scripts or raw filesystem roots", enforced by
    the shape rather than by a validation pass someone can forget to call.
    """

    mode: CleanupMode
    #: Only ids in `REGISTERED_RULES` may appear. An unknown id is a refusal at
    #: construction, because a policy naming a rule nobody implemented would
    #: otherwise read as coverage the tool does not have.
    enabled_rules: tuple[str, ...]
    #: POSIX-style relative path globs. A match excludes the file from cleanup
    #: entirely, in every mode.
    excluded_paths: tuple[str, ...] = ()
    #: The runtime the owner measured this policy against. Recorded in every
    #: receipt; a mismatch with the running interpreter is a refusal.
    python_version: str | None = None
    #: The optimization levels the owner requires. Defaults to all of them,
    #: because requiring fewer would be the owner narrowing the plan's own gate.
    optimize_levels: tuple[int, ...] = REQUIRED_OPTIMIZE_LEVELS

    def __post_init__(self) -> None:
        unknown = [rule for rule in self.enabled_rules if rule not in CLEANUP_RULE_IDS]
        if unknown:
            raise PolicyRefused(
                f"policy enables unknown rule id(s) {unknown}; the rules this build "
                f"can apply are {list(CLEANUP_RULE_IDS)}"
            )
        missing = [level for level in REQUIRED_OPTIMIZE_LEVELS
                   if level not in self.optimize_levels]
        if missing:
            raise PolicyRefused(
                f"policy omits optimization level(s) {missing}. The plan requires "
                f"{list(REQUIRED_OPTIMIZE_LEVELS)} and an owner may add to them; a "
                "level that is not compared is a level nothing was proved at"
            )

    def to_json(self) -> dict[str, Any]:
        """The policy as an owner reads and writes it.

        These are the same key names `parse_policy` accepts, so a policy
        survives a round trip through JSON. They were camelCase before, which
        meant `parse_policy(self.to_json())` refused the policy's own output and
        an owner had to spell all five keys by hand to configure cleanup at all.

        `digest` hashes this document, so renaming the keys changes every
        recorded policy digest. Nothing has been applied under a real policy
        yet, and a digest that cannot be reproduced from the document is worse
        than one that changes once.
        """
        return {
            "mode": self.mode.value,
            "enabled_rules": list(self.enabled_rules),
            "excluded_paths": list(self.excluded_paths),
            "python_version": self.python_version,
            "optimize_levels": list(self.optimize_levels),
        }

    @property
    def digest(self) -> str:
        """The identity of this policy, so a receipt can name the policy in force."""
        canonical = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def may_write(self) -> bool:
        """Whether this policy authorizes a write at all."""
        return self.mode.value in APPLY_MODES

    def rules_for(self, relative_path: str) -> tuple[str, ...]:
        """The rules this policy authorizes for one path, exclusions applied."""
        if is_excluded(relative_path, self.excluded_paths):
            return ()
        return tuple(self.enabled_rules)

    def admits(self, relative_path: str, rule_id: str) -> bool:
        """Whether this policy authorizes this one rule on this one path."""
        return rule_id in self.rules_for(relative_path)

    def check_runtime(self, python_version: str) -> str | None:
        """The reason this policy cannot govern the running interpreter, or None."""
        measured = self.python_version or _runtime_policy_version()
        if python_version != measured:
            return (
                f"cleanup policy was approved against CPython {measured} and this "
                f"interpreter is {python_version}; the compiled-equality claim is "
                "scoped to the compiler that produced the code objects"
            )
        if python_version not in TESTED_CPYTHON_VERSIONS:
            return (
                f"CPython {python_version} has not been measured; the measured "
                f"versions are {sorted(TESTED_CPYTHON_VERSIONS)}"
            )
        return None


class PolicyRefused(Exception):
    """The policy itself is not usable, so nothing derived from it may run."""


def _runtime_policy_version() -> str:
    """The interpreter this process is running, for the default policy binding."""
    import platform

    return platform.python_version()


def is_excluded(relative_path: str, patterns: Sequence[str]) -> bool:
    """Whether a repository-relative path matches any exclusion pattern.

    Matching is segment-wise, and a pattern may start at any segment. `vendor`
    excludes `vendor/a.py` and `src/vendor/a.py` alike, because a rule that
    excluded only the top-level directory would leave the same directory nested
    one level down fully cleaned, which is not what an owner who wrote `vendor`
    meant. Segments rather than a raw `fnmatch` over the whole path, so
    `vendor` does not exclude `vendor_helper.py`: a rule that excluded more than
    it said would be a rule nobody could reason about.

    A pattern with slashes is anchored to the root, because a pattern like
    `src/vendor` names one specific path rather than any directory called
    `vendor`.
    """
    if not patterns:
        return False
    segments = relative_path.replace("\\", "/").split("/")
    for pattern in patterns:
        cleaned = pattern.replace("\\", "/").strip("/")
        if not cleaned:
            continue
        parts = cleaned.split("/")
        if len(parts) > len(segments):
            continue
        starts = (0,) if len(parts) > 1 else range(len(segments))
        if any(
            all(
                fnmatch.fnmatchcase(segment, part)
                for segment, part in zip(segments[start:], parts)
            )
            for start in starts
        ):
            return True
    return False


@dataclass(frozen=True)
class PolicyRefusal:
    """Why this policy does not govern this request. Named, never a flag."""

    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"result": "REFUSED", "reason": self.reason, "detail": self.detail}


def check_policy(
    policy: CleanupPolicy,
    relative_path: str,
    rule_id: str | None = None,
    *,
    python_version: str | None = None,
) -> PolicyRefusal | None:
    """Every reason this policy cannot authorize a request, checked in one place.

    Ordered so the refusal a reader sees is the most fundamental one: a mode that
    cannot write beats a path that is excluded, and an excluded path beats an
    unknown rule. A caller therefore learns the reason it can most act on.
    """
    if policy.mode is CleanupMode.OFF:
        return PolicyRefusal(
            "cleanup_off",
            "cleanup is off for this project; enable a mode through approved policy",
        )
    if is_excluded(relative_path, policy.excluded_paths):
        return PolicyRefusal(
            "path_excluded",
            f"{relative_path} matches an approved exclusion, so no rule applies to it",
        )
    if rule_id is not None and not policy.admits(relative_path, rule_id):
        return PolicyRefusal(
            "rule_not_enabled",
            f"rule {rule_id!r} is not enabled for {relative_path}; the policy enables "
            f"{list(policy.rules_for(relative_path))}",
        )
    if python_version is not None:
        mismatch = policy.check_runtime(python_version)
        if mismatch is not None:
            return PolicyRefusal("runtime_not_approved", mismatch)
    if Path(relative_path).suffix.lower() in ECMASCRIPT_SUFFIXES:
        return PolicyRefusal(
            "unsupported_language",
            f"{relative_path} is JavaScript or TypeScript, which stay unsupported for "
            "automatic cleanup until a parser and a preservation adapter exist (Plan 11)",
        )
    return None


#: The policy a caller gets when it has approved none. `off` is the correct
#: default for a system that writes files: absence of policy is not permission.
OFF_POLICY = CleanupPolicy(mode=CleanupMode.OFF, enabled_rules=())

#: A policy object that proposes and never writes, for the preview path.
PREVIEW_POLICY = CleanupPolicy(
    mode=CleanupMode.PREVIEW, enabled_rules=CLEANUP_RULE_IDS
)


def parse_policy(document: Any) -> CleanupPolicy:
    """Build a policy from the JSON an owner approved, refusing anything unusable.

    The one place untrusted data becomes a typed value. Everything the policy
    can express is validated here; a caller holding a `CleanupPolicy` can trust
    it without re-checking, and a malformed policy is a refusal at the boundary
    rather than a surprise three layers down.
    """
    if not isinstance(document, dict):
        raise PolicyRefused("a cleanup policy must be a JSON object")
    unknown = sorted(set(document) - {
        "mode", "enabled_rules", "excluded_paths", "python_version", "optimize_levels",
    })
    if unknown:
        raise PolicyRefused(
            f"unsupported policy key(s) {unknown}; a policy declares mode, "
            "enabled_rules, excluded_paths, python_version and optimize_levels"
        )
    mode_value = document.get("mode")
    try:
        mode = CleanupMode(mode_value)
    except ValueError as exc:
        raise PolicyRefused(
            f"unknown cleanup mode {mode_value!r}; the modes are {list(CLEANUP_MODES)}"
        ) from exc

    def string_tuple(key: str) -> tuple[str, ...]:
        # Absent is an empty tuple, and only an ABSENT key defaults. A key that is
        # present must hold a real list, so `{"excluded_paths": ""}` is a
        # malformed policy rather than an empty one.
        if key not in document:
            return ()
        value = document[key]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise PolicyRefused(f"{key} must be a list of nonempty strings")
        return tuple(value)

    levels = document.get("optimize_levels", list(REQUIRED_OPTIMIZE_LEVELS))
    if not isinstance(levels, list) or any(not isinstance(level, int) for level in levels):
        raise PolicyRefused(
            f"optimize_levels must be a list of integers; the checker compares "
            f"{list(REQUIRED_OPTIMIZE_LEVELS)}"
        )
    version = document.get("python_version")
    if version is not None and (not isinstance(version, str) or not version):
        raise PolicyRefused("python_version must be a nonempty string when given")

    return CleanupPolicy(
        mode=mode,
        enabled_rules=string_tuple("enabled_rules"),
        excluded_paths=string_tuple("excluded_paths"),
        python_version=version,
        optimize_levels=tuple(levels),
    )