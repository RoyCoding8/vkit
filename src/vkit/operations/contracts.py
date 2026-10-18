"""One compatibility question with verifier-owned format profiles."""
from __future__ import annotations

from pathlib import Path
from typing import Any

PROTOBUF_PROFILES = {
    "protobuf-file": "FILE",
    "protobuf-package": "PACKAGE",
    "protobuf-wire-json": "WIRE_JSON",
    "protobuf-wire": "WIRE",
}
CONTRACT_PROFILES = ("openapi", *PROTOBUF_PROFILES, "json-containment", "typescript-api")


def compare_contracts(root: Path, old_path: str, new_path: str, profile: str,
                      timeout_seconds: int = 20) -> dict[str, Any]:
    if profile == "openapi":
        from .openapi import check_openapi_compatibility

        result = check_openapi_compatibility(root, old_path, new_path, timeout_seconds)
    elif profile in PROTOBUF_PROFILES:
        from .protobuf import check_proto_compatibility

        result = check_proto_compatibility(root, old_path, new_path, PROTOBUF_PROFILES[profile], timeout_seconds)
    elif profile == "json-containment":
        from .json_containment import compare_json_containment

        result = compare_json_containment(root, old_path, new_path, timeout_seconds)
    elif profile == "typescript-api":
        from .typescript_api import check_typescript_api_compatibility

        result = check_typescript_api_compatibility(root, old_path, new_path, timeout_seconds)
    else:
        raise ValueError(f"profile must be one of {', '.join(CONTRACT_PROFILES)}")
    return {**result, "operation": "compare_contracts", "profile": profile}
