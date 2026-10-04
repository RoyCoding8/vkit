"""One headless browser flow against the installed wheel in Ubuntu CI."""
from __future__ import annotations

import json
from contextlib import closing
import os
import shutil
import sys
from pathlib import Path

import pytest
import subproc

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
CHECK_ID = "totals-behavior"
HOSTILE_DESCRIPTION = '<img src=x onerror="window.__vkitPwned=true">'

pytestmark = pytest.mark.skipif(
    not os.environ.get("VKIT_INSTALL_ROOT"),
    reason="the installed-wheel browser flow runs in its dedicated GitHub Actions job",
)


def test_installed_console_settings_run_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the installed console's settings, actions, and evidence in a browser."""
    install_root = Path(os.environ["VKIT_INSTALL_ROOT"]).resolve()
    for name in list(sys.modules):
        if name == "vkit" or name.startswith("vkit."):
            del sys.modules[name]
    sys.path[:] = [
        entry for entry in sys.path
        if Path(entry or ".").resolve() != (REPO_ROOT / "src").resolve()
    ]
    sys.path.insert(0, str(install_root))
    monkeypatch.setenv("PYTHONPATH", str(install_root))

    import vkit
    from vkit.console import operations, server

    package_path = Path(vkit.__file__).resolve()
    assert package_path.is_relative_to(install_root), (
        f"the browser test imported {package_path}, not the installed wheel at {install_root}"
    )
    assert not package_path.is_relative_to(REPO_ROOT / "src"), package_path

    repo = tmp_path / "browser repo"
    shutil.copytree(EXAMPLE, repo)
    manifest_path = repo / "verification" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checks"][0]["description"] = HOSTILE_DESCRIPTION
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    subproc.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True, timeout=30)
    subproc.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, timeout=30)
    subproc.run(
        ["git", "-c", "user.email=browser@vkit.test", "-c", "user.name=Browser CI",
         "commit", "-qm", "browser fixture"],
        cwd=repo, check=True, capture_output=True,
        timeout=30,
    )

    httpd, _thread = server.start_in_thread(operations.open_context(repo), port=0)
    page_errors: list[str] = []
    try:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            with closing(playwright.chromium.launch()) as browser:
                page = browser.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                static_status: dict[str, int] = {}

                def record_static(response) -> None:
                    if response.url.endswith(("/app.js", "/style.css")):
                        static_status[response.url.rsplit("/", 1)[-1]] = response.status

                page.on("response", record_static)
                response = page.goto(
                    f"http://127.0.0.1:{httpd.server_address[1]}/",
                    wait_until="networkidle",
                )
                assert response is not None and response.status == 200
                assert static_status == {"app.js": 200, "style.css": 200}, static_status
                expect(page.get_by_role("heading", name="vkit console")).to_be_visible()

                available = page.locator("#home-available")
                expect(available.get_by_text(HOSTILE_DESCRIPTION, exact=True)).to_be_visible()
                assert available.locator("img").count() == 0
                assert page.evaluate("() => window.__vkitPwned") is None

                settings = page.get_by_role("button", name="Settings")
                settings.focus()
                page.keyboard.press("Enter")
                expect(settings).to_have_attribute("aria-current", "page")
                editor = page.get_by_role("textbox", name="proposed configuration document")
                expect(editor).to_be_visible()
                expect(page.get_by_text("verification settings applied", exact=False)).to_be_visible()
                expect(page.get_by_text("configuration digest", exact=True)).to_be_visible()
                expect(page.get_by_text("Every field is validated before it is written", exact=False)).to_be_visible()

                draft = json.loads(editor.input_value())
                draft["description"] = "strict configuration for browser acceptance"
                draft["cleanup"] = {
                    "mode": "apply_verified",
                    "enabled_rules": ["ORDINARY_TRAILING_COMMENT"],
                    "excluded_paths": [],
                }
                editor.fill(json.dumps(draft, indent=2))
                page.get_by_role("button", name="Preview this change").click()
                expect(page.locator("#integrations-body").get_by_text("cleanup.mode", exact=True)).to_be_visible()
                page.get_by_role("button", name="Save this proposal").click()
                toast = page.locator("#toast")
                expect(toast).to_contain_text("saved candidate", timeout=15_000)
                expect(page.locator("#integrations-body [aria-busy='true']")).to_have_count(0)

                draft = json.loads(editor.input_value())
                obligations = draft["required_checks"][0]["obligations"]
                draft["required_checks"][0]["obligations"] = obligations[:2]
                draft["cleanup"] = {"mode": "off", "enabled_rules": [], "excluded_paths": []}
                editor.fill(json.dumps(draft, indent=2))

                base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
                current = page.request.get(f"{base_url}/api/project")
                assert current.ok
                expected_digest = current.json()["configuration"]["digest"]
                with page.expect_request(
                    lambda request: request.method == "POST"
                    and "/api/apply?" in request.url
                    and "stage=preview" in request.url
                ) as preview_request:
                    page.get_by_role("button", name="Preview this change").click()
                payload = preview_request.value.post_data_json
                assert payload["expected_digest"] == expected_digest
                assert payload["document"] == draft

                expect(
                    page.get_by_text("this document change needs review", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("requirements this document stops listing", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(f"{CHECK_ID}: case 'several-positives'", exact=True)).to_be_visible()
                expect(page.get_by_text("removes cleanup write authority", exact=True)).to_be_visible()
                expect(
                    page.locator("#integrations-body").get_by_text("cleanup.mode", exact=True)
                ).to_be_visible()
                expect(page.get_by_text("Run evidence is unchanged", exact=False)).to_be_visible()

                page.get_by_role("button", name="Checks").click()
                run = page.get_by_role("button", name=f"Run {CHECK_ID}")
                expect(run).to_be_visible()
                run.click()
                expect(toast).to_contain_text("run ", timeout=120_000)
                expect(toast).to_contain_text("PASS", timeout=120_000)

                monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
                page.get_by_role("button", name="Evidence").click()
                evidence = page.locator("#evidence-body")
                expect(evidence.get_by_text("python is not on PATH", exact=False)).to_be_visible()
                expect(evidence.get_by_role("button", name=f"Run {CHECK_ID}")).to_have_count(0)
                expect(evidence.locator("strong.mono").filter(has_text=CHECK_ID)).to_be_visible()
                expect(evidence.get_by_text("scenario PASS", exact=False)).to_be_visible()
                expect(evidence.get_by_text("case empty-cart", exact=True)).to_be_visible()
                assert not page_errors, f"browser page errors: {page_errors}"

                screenshot_dir = os.environ.get("VKIT_SCREENSHOT_DIR")
                if screenshot_dir:
                    destination = Path(screenshot_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / "console-evidence.png"), full_page=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
