"""Protected integration decisions pin external expected observations."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures as fx  # noqa: E402
from test_integration import verify  # noqa: E402
from vkit.integration.oracle import expectation_identity  # noqa: E402


def expectation_repository(root: Path, *, declaration: bool) -> Path:
    repo = fx.build_repository(root)
    driver = repo / fx.DRIVER_NAME
    source, count = re.subn(
        r"(?m)^CASES = .*?$",
        "CASES = json.loads(Path('expected.json').read_text(encoding='utf-8'))",
        driver.read_text(encoding="utf-8"),
    )
    assert count == 1
    driver.write_text(source, encoding="utf-8")
    fx.write_json(repo / "expected.json", fx.CASES)

    manifest_path = repo / fx.MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check = manifest["checks"][0]
    check["inputs"].append("expected.json")
    if declaration:
        check["expectations"] = ["expected.json"]
    else:
        check.pop("expectations", None)
    fx.write_json(manifest_path, manifest)

    approved = fx.commit_all(repo, "approve the external expectation file")
    fx.write_json(repo / fx.POLICY_NAME, fx.policy_document(manifest_revision=approved))
    fx.commit_all(repo, "pin the approved expectation boundary")
    return repo


def _attack(repo: Path) -> str:
    fx.checkout_branch(repo, "candidate", fx.main_revision(repo))
    quote = repo / fx.QUOTE_NAME
    source = quote.read_text(encoding="utf-8")
    assert source.count('    print(result["total"])') == 1
    quote.write_text(source.replace('    print(result["total"])', "    print(0)"), encoding="utf-8")
    altered = [(scenario, tail, "0") for scenario, tail, _ in fx.CASES]
    fx.write_json(repo / "expected.json", altered)
    return fx.commit_all(repo, "lower the application and its expectation file")


def test_omitted_expectation_classification_refuses_the_original_attack(tmp_path: Path) -> None:
    repo = expectation_repository(tmp_path, declaration=False)

    done, record = verify(repo, _attack(repo), "main", "@main")

    assert record["decision"] == "REJECTED"
    assert done.returncode != 0
    assert record["checks"] == []
    assert any(f["kind"] == "expectation_boundary_unresolved" for f in record["findings"])


def test_changed_approved_expectation_bytes_refuse_before_checks_run(tmp_path: Path) -> None:
    repo = expectation_repository(tmp_path, declaration=True)
    candidate = _attack(repo)

    done, record = verify(repo, candidate, "main", "@main")

    assert record["decision"] == "REJECTED"
    assert done.returncode != 0
    assert record["checks"] == []
    assert any(f["kind"] == "expectation_bytes_changed" for f in record["findings"])
    expected = next(i for i in record["fixture"]["inputs"] if i["path"] == "expected.json")
    assert expected["role"] == "expectation"
    assert expected["sha256"] == hashlib.sha256((repo / "expected.json").read_bytes()).hexdigest()


def test_approved_policy_change_can_pin_new_expectation_bytes(tmp_path: Path) -> None:
    repo = expectation_repository(tmp_path, declaration=True)
    expected_path = repo / "expected.json"
    old_bytes = expected_path.read_bytes()
    new_bytes = (json.dumps(fx.CASES, indent=4) + "\n").encode("utf-8")
    assert old_bytes != new_bytes
    expected_path.write_bytes(new_bytes)
    approved = fx.commit_all(repo, "review updated expected observations")
    policy = json.loads((repo / fx.POLICY_NAME).read_text(encoding="utf-8"))
    policy["manifest_revision"] = approved
    fx.write_json(repo / fx.POLICY_NAME, policy)
    fx.commit_all(repo, "approve the new expectation revision")

    fx.checkout_branch(repo, "candidate", fx.main_revision(repo))
    product = repo / fx.RULES_NAME
    product.write_text(product.read_text(encoding="utf-8") + "\n# reviewed oracle change\n", encoding="utf-8")
    candidate = fx.commit_all(repo, "change product after oracle review")
    done, record = verify(repo, candidate, "main", "@main")

    assert done.returncode == 0
    assert record["decision"] == "ACCEPTED"
    expected = next(i for i in record["fixture"]["inputs"] if i["path"] == "expected.json")
    assert expected["sha256"]
    oracle = record["verifier"]["verification_context"]
    assert oracle["candidate_digests"]["expected.json"] == oracle["approved_digests"]["expected.json"]
    assert oracle["candidate_digests"]["expected.json"] != hashlib.sha256(old_bytes).hexdigest()
    product_input = next(i for i in record["fixture"]["inputs"] if i["path"] == fx.RULES_NAME)
    assert product_input["role"] == "product"


def test_candidate_driver_change_stays_blocked_and_product_inputs_can_change(
    tmp_path: Path,
) -> None:
    repo = fx.build_repository(tmp_path)
    fx.checkout_branch(repo, "forged-driver", fx.main_revision(repo))
    driver = repo / fx.DRIVER_NAME
    driver.write_text(driver.read_text(encoding="utf-8") + "\n# candidate edit\n", encoding="utf-8")
    forged = fx.commit_all(repo, "edit the candidate's driver")
    done, record = verify(repo, forged, "main", "@main")
    assert done.returncode != 0
    assert record["decision"] == "REJECTED"
    assert record["checks"] == []
    assert any(f["kind"] == "checker_bytes_changed" for f in record["findings"])

    fx.checkout_branch(repo, "ordinary-product", fx.main_revision(repo))
    product = repo / fx.RULES_NAME
    product.write_text(product.read_text(encoding="utf-8") + "\n# product change\n", encoding="utf-8")
    candidate = fx.commit_all(repo, "make a harmless product change")
    done, record = verify(repo, candidate, "main", "@main")
    assert done.returncode == 0
    assert record["decision"] == "ACCEPTED"
    assert next(i for i in record["fixture"]["inputs"] if i["path"] == fx.RULES_NAME)["role"] == "product"


def test_expectation_identity_hashes_the_exact_checkout_bytes(tmp_path: Path) -> None:
    raw = b'{"total": 7}\r\n'
    (tmp_path / "expected.json").write_bytes(raw)

    assert expectation_identity(tmp_path, {"expected.json"}) == {
        "expected.json": hashlib.sha256(raw).hexdigest()
    }
