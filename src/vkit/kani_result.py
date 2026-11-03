"""Classify a qualified Kani transcript; this does not authenticate source completeness."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_HARNESS_BYTES = 256 * 1024
_HARNESS = "vkit_relational"
_ASSERTIONS = {
    "base_observation_model",
    "candidate_observation_model",
    "candidate_standing_intent",
    "relational_policy",
}
_PROPERTY_STATUSES = {
    "Success",
    "Failure",
    "Unreachable",
    "Undetermined",
    "SolverError",
}
_COUNT_FIELDS = (
    "total_properties passed failed unreachable undetermined "
    "solver_error satisfied unsatisfiable covered uncovered"
).split()
_PROFILE = {
    "schema": "1.0",
    "kani": "0.68.0",
    "target": "x86_64-unknown-linux-gnu",
    "build_mode": "release",
    "rustc": "rustc 1.100.0-nightly (8925ea358 2026-08-20)",
    "cbmc": "6.11.0 (cbmc-6.11.0)",
    "goto_cc": "gcc (goto-cc 6.11.0 (cbmc-6.11.0)) 15.2.0",
    "goto_instrument": "6.11.0 (cbmc-6.11.0)",
}


@dataclass(frozen=True, slots=True)
class KaniOutcome:
    kind: Literal["verified", "proof_rejected", "unknown"]
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "source_proof": False,
            "approval_authority": False,
        }


class _InvalidResult(ValueError):
    pass


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidResult("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise _InvalidResult("invalid_json_number")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _InvalidResult("malformed_export")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _InvalidResult("malformed_export")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _InvalidResult("invalid_counter")
    return value


def _required(mapping: dict[str, object], key: str) -> object:
    if key not in mapping:
        raise _InvalidResult("missing_export_field")
    return mapping[key]


def _rust_string_literal(literal: str) -> str:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        raise _InvalidResult("unexpected_harness_module")
    result: list[str] = []
    index = 1
    while index < len(literal) - 1:
        char = literal[index]
        index += 1
        if char == "\\":
            if index >= len(literal) - 1:
                raise _InvalidResult("unexpected_harness_module")
            escape = literal[index]
            index += 1
            if escape in {'"', "\\"}:
                result.append(escape)
            elif escape == "u" and index < len(literal) - 1 and literal[index] == "{":
                end = literal.find("}", index + 1)
                digits = literal[index + 1 : end] if end >= 0 else ""
                if (
                    end < 0
                    or end >= len(literal) - 1
                    or not 1 <= len(digits) <= 6
                    or re.fullmatch(r"[0-9a-f]+", digits) is None
                ):
                    raise _InvalidResult("unexpected_harness_module")
                codepoint = int(digits, 16)
                if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    raise _InvalidResult("unexpected_harness_module")
                result.append(chr(codepoint))
                index = end + 1
            else:
                raise _InvalidResult("unexpected_harness_module")
        elif char == '"' or ord(char) < 32 or ord(char) == 127:
            raise _InvalidResult("unexpected_harness_module")
        else:
            result.append(char)
    return "".join(result)


def _derive_harness(harness: bytes) -> tuple[dict[str, int], dict[str, str]]:
    if len(harness) > _MAX_HARNESS_BYTES:
        raise _InvalidResult("oversized_harness")
    try:
        source = harness.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidResult("invalid_harness_encoding") from exc
    lines = source.splitlines()
    proof_lines = [
        i for i, line in enumerate(lines) if line.strip() == "#[kani::proof]"
    ]
    functions = [
        i for i, line in enumerate(lines) if line.strip() == "fn vkit_relational() {"
    ]
    if (
        len(proof_lines) != 1
        or len(functions) != 1
        or functions[0] != proof_lines[0] + 1
    ):
        raise _InvalidResult("unexpected_harness")

    module_paths: dict[str, str] = {}
    module_pattern = re.compile(
        r'^\s*#\[path = ("(?:[^"\\]|\\.)*")\] mod vkit_(base|candidate);\s*$'
    )
    for line in lines:
        if "#[path" not in line:
            continue
        match = module_pattern.fullmatch(line)
        if match is None or match.group(2) in module_paths:
            raise _InvalidResult("unexpected_harness_module")
        module_paths[match.group(2)] = _rust_string_literal(match.group(1))
    if set(module_paths) != {"base", "candidate"} or any(
        not path for path in module_paths.values()
    ):
        raise _InvalidResult("unexpected_harness_module")

    assertions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if "assert!" not in line:
            continue
        match = re.fullmatch(r'\s*assert!\(.*?,\s*"([a-z_]+)"\);\s*', line)
        if match is None:
            raise _InvalidResult("unexpected_harness_assertion")
        name = match.group(1)
        if name not in _ASSERTIONS or name in assertions:
            raise _InvalidResult("unexpected_harness_assertion")
        assertions[name] = index + 1
    if set(assertions) != _ASSERTIONS:
        raise _InvalidResult("missing_harness_assertion")
    return assertions, module_paths


def _check_profile(
    document: dict[str, object], assertion_lines: dict[str, int]
) -> None:
    metadata = _mapping(_required(document, "metadata"))
    tools = _mapping(_required(document, "tools"))
    if (
        metadata.get("version") != _PROFILE["schema"]
        or metadata.get("kani_version") != _PROFILE["kani"]
        or metadata.get("target") != _PROFILE["target"]
        or metadata.get("build_mode") != _PROFILE["build_mode"]
        or tools.get("kani") != metadata.get("kani_version")
        or tools.get("rustc") != _PROFILE["rustc"]
        or tools.get("cbmc") != _PROFILE["cbmc"]
        or tools.get("goto_cc") != _PROFILE["goto_cc"]
        or tools.get("goto_instrument") != _PROFILE["goto_instrument"]
        or tools.get("solvers") != [{"name": "cadical", "version": None}]
    ):
        raise _InvalidResult("unsupported_profile")

    coverage = _mapping(_required(document, "coverage"))
    if coverage.get("enabled") is not False:
        raise _InvalidResult("unsupported_coverage_mode")

    harnesses = _list(_required(document, "harness_metadata"))
    if len(harnesses) != 1:
        raise _InvalidResult("unexpected_harness_count")
    harness = _mapping(harnesses[0])
    source = _mapping(_required(harness, "source"))
    attributes = _mapping(_required(harness, "attributes"))
    contract = _mapping(_required(harness, "contract"))
    if (
        harness.get("pretty_name") != _HARNESS
        or not isinstance(harness.get("mangled_name"), str)
        or not harness.get("mangled_name")
        or harness.get("crate_name") != "harness"
        or source.get("file") != "harness.rs"
        or attributes.get("kind") != "Proof"
        or attributes.get("should_panic") is not False
        or contract.get("contracted_function_name") is not None
        or contract.get("recursion_tracker") is not None
        or harness.get("has_loop_contracts") is not False
        or harness.get("is_automatically_generated") is not False
        or harness.get("is_bounded") is not False
        or harness.get("is_ctor_based") is not False
    ):
        raise _InvalidResult("unexpected_harness_metadata")
    start_line = _integer(_required(source, "start_line"))
    end_line = _integer(_required(source, "end_line"))
    if any(not start_line <= line <= end_line for line in assertion_lines.values()):
        raise _InvalidResult("harness_source_range_mismatch")

    errors = _list(_required(document, "error_details"))
    properties = _list(_required(document, "property_details"))
    cbmc = _list(_required(document, "cbmc"))
    if len(errors) != 1 or len(properties) != 1 or len(cbmc) != 1:
        raise _InvalidResult("unexpected_harness_count")
    for item in (errors[0], properties[0], cbmc[0]):
        if _mapping(item).get("harness_id") != _HARNESS:
            raise _InvalidResult("harness_identity_mismatch")

    cbmc_item = _mapping(cbmc[0])
    cbmc_metadata = _mapping(_required(cbmc_item, "cbmc_metadata"))
    configuration = _mapping(_required(cbmc_item, "configuration"))
    if (
        cbmc_metadata.get("version") != "6.11.0"
        or cbmc_metadata.get("os_info") != "x86_64 linux unix"
        or configuration.get("solver") != "cadical"
        or configuration.get("object_bits") != 16
    ):
        raise _InvalidResult("unsupported_cbmc_profile")


def _check_properties(
    document: dict[str, object],
    assertion_lines: dict[str, int],
    module_paths: dict[str, str],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    results = _mapping(_required(document, "verification_results"))
    summary = _mapping(_required(results, "summary"))
    rows = _list(_required(results, "results"))
    if len(rows) != 1:
        raise _InvalidResult("unexpected_harness_count")
    row = _mapping(rows[0])
    if row.get("harness_id") != _HARNESS:
        raise _InvalidResult("harness_identity_mismatch")
    if summary.get("status") != "completed":
        raise _InvalidResult("incomplete_summary")
    if (
        _integer(_required(summary, "total_harnesses")) != 1
        or _integer(_required(summary, "executed")) != 1
    ):
        raise _InvalidResult("inconsistent_summary")

    checks = [_mapping(check) for check in _list(_required(row, "checks"))]
    if not checks:
        raise _InvalidResult("missing_properties")
    ids: list[int] = []
    named: dict[str, str] = {}
    status_counts = {status: 0 for status in _PROPERTY_STATUSES}
    for check in checks:
        check_id = _required(check, "id")
        if isinstance(check_id, bool) or not isinstance(check_id, int) or check_id < 1:
            raise _InvalidResult("invalid_property_id")
        ids.append(check_id)
        status = _required(check, "status")
        if status not in _PROPERTY_STATUSES:
            raise _InvalidResult("unknown_property_status")
        status_counts[status] += 1
        if check.get("category") != "assertion":
            raise _InvalidResult("unknown_property_category")
        function = _required(check, "function")
        description = _required(check, "description")
        location = _mapping(_required(check, "location"))
        if not isinstance(function, str) or not isinstance(description, str):
            raise _InvalidResult("malformed_property")
        file = location.get("file")
        line = location.get("line")
        column = location.get("column")
        if not all(
            isinstance(value, str) and value.isdecimal() for value in (line, column)
        ):
            raise _InvalidResult("malformed_property_location")
        if function == _HARNESS:
            name = (
                description[1:-1]
                if description.startswith('"') and description.endswith('"')
                else ""
            )
            if (
                name not in _ASSERTIONS
                or name in named
                or file != "harness.rs"
                or int(line) != assertion_lines[name]
                or column != "1"
            ):
                raise _InvalidResult("harness_assertion_mismatch")
            named[name] = str(status)
        else:
            prefix_to_file = {
                "vkit_candidate::": module_paths["candidate"],
                "vkit_base::": module_paths["base"],
            }
            expected_file = next(
                (
                    path
                    for prefix, path in prefix_to_file.items()
                    if function.startswith(prefix)
                ),
                None,
            )
            if expected_file is None or file != expected_file or not description:
                raise _InvalidResult("unexpected_source_property")

    if len(ids) != len(set(ids)) or set(ids) != set(range(1, len(ids) + 1)):
        raise _InvalidResult("duplicate_or_missing_property_id")
    if set(named) != _ASSERTIONS:
        raise _InvalidResult("missing_harness_assertion")

    detail = _mapping(
        _required(
            _mapping(_list(_required(document, "property_details"))[0]),
            "property_details",
        )
    )
    counts = {key: _integer(_required(detail, key)) for key in _COUNT_FIELDS}
    if counts["total_properties"] != len(checks):
        raise _InvalidResult("property_total_mismatch")
    expected_counts = {
        "passed": status_counts["Success"],
        "failed": status_counts["Failure"],
        "unreachable": status_counts["Unreachable"],
        "undetermined": status_counts["Undetermined"],
        "solver_error": status_counts["SolverError"],
    }
    if any(counts[key] != value for key, value in expected_counts.items()) or any(
        counts[key] != 0
        for key in ("satisfied", "unsatisfiable", "covered", "uncovered")
    ):
        raise _InvalidResult("property_counter_mismatch")
    if sum(expected_counts.values()) != counts["total_properties"]:
        raise _InvalidResult("property_counter_mismatch")

    failed = (
        status_counts["Failure"]
        + status_counts["Unreachable"]
        + status_counts["Undetermined"]
        + status_counts["SolverError"]
    )
    expected_result_status = "Success" if failed == 0 else "Failure"
    if row.get("status") != expected_result_status:
        raise _InvalidResult("result_status_mismatch")
    summary_counts = {
        "successful": 1 if expected_result_status == "Success" else 0,
        "failed": 1 if expected_result_status == "Failure" else 0,
    }
    if any(
        _integer(_required(summary, key)) != value
        for key, value in summary_counts.items()
    ):
        raise _InvalidResult("summary_counter_mismatch")
    error = _mapping(_list(_required(document, "error_details"))[0])
    has_errors = _required(error, "has_errors")
    if not isinstance(has_errors, bool) or has_errors != (
        expected_result_status == "Failure"
    ):
        raise _InvalidResult("error_summary_mismatch")
    if has_errors and (
        error.get("error_type") != "assertion_failure"
        or error.get("failed_properties_type") != "PanicsOnly"
        or error.get("exit_status") != "properties_failed"
    ):
        raise _InvalidResult("error_summary_mismatch")
    if not has_errors and any(
        key in error for key in ("error_type", "failed_properties_type", "exit_status")
    ):
        raise _InvalidResult("error_summary_mismatch")
    return checks, named


def classify_kani_result(raw: bytes, *, exit_code: int, harness: bytes) -> KaniOutcome:
    if not isinstance(raw, bytes) or len(raw) > _MAX_RESULT_BYTES:
        return KaniOutcome("unknown", "invalid_or_oversized_export")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not isinstance(harness, bytes)
    ):
        return KaniOutcome("unknown", "invalid_decoder_input")
    try:
        document = _mapping(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_object,
                parse_constant=_reject_constant,
            )
        )
        assertion_lines, module_paths = _derive_harness(harness)
        _check_profile(document, assertion_lines)
        checks, assertions = _check_properties(document, assertion_lines, module_paths)
    except (
        _InvalidResult,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return KaniOutcome("unknown", "malformed_or_inconsistent_export")

    statuses = [check["status"] for check in checks]
    if any(status in {"Undetermined", "SolverError"} for status in statuses):
        return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
    failures = [check for check in checks if check["status"] == "Failure"]
    if not failures:
        if "Unreachable" in statuses:
            return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
        if exit_code == 0:
            return KaniOutcome("verified", "all_qualified_properties_succeeded")
        return KaniOutcome("unknown", "exit_status_mismatch")

    failed_assertion = next(
        (name for name, status in assertions.items() if status == "Failure"), None
    )
    if (
        exit_code == 1
        and len(failures) == 1
        and failed_assertion in {"candidate_standing_intent", "relational_policy"}
        and assertions["base_observation_model"] == "Success"
        and assertions["candidate_observation_model"] == "Success"
    ):
        failed_line = assertion_lines[failed_assertion]
        for check in checks:
            if check["status"] == "Unreachable":
                description = check.get("description", "")
                name = (
                    description[1:-1]
                    if isinstance(description, str)
                    and description.startswith('"')
                    and description.endswith('"')
                    else ""
                )
                if (
                    check.get("function") != _HARNESS
                    or name not in _ASSERTIONS
                    or assertion_lines[name] <= failed_line
                ):
                    return KaniOutcome(
                        "unknown", "incomplete_or_undetermined_properties"
                    )
        if all(
            check["status"] in {"Success", "Unreachable"}
            or (
                check["status"] == "Failure"
                and check.get("function") == _HARNESS
                and check.get("description") == f'"{failed_assertion}"'
            )
            for check in checks
        ):
            return KaniOutcome(
                "proof_rejected", f"named_{failed_assertion}_assertion_failed"
            )

    if any(
        check["function"] != _HARNESS and check["status"] == "Failure"
        for check in checks
    ):
        return KaniOutcome("unknown", "safety_property_failed")
    if (
        assertions["base_observation_model"] == "Failure"
        or assertions["candidate_observation_model"] == "Failure"
    ):
        return KaniOutcome("unknown", "observation_model_failed")
    if "Unreachable" in statuses:
        return KaniOutcome("unknown", "incomplete_or_undetermined_properties")
    return KaniOutcome("unknown", "model_or_safety_property_failed")
