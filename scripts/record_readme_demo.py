from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CapturedStep:
    caption: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float


def capture(
    steps: list[CapturedStep],
    caption: str,
    argv: Sequence[str],
    project: Path,
    environment: dict[str, str],
) -> CapturedStep:
    started = time.monotonic()
    result = subprocess.run(
        list(argv),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    step = CapturedStep(
        caption=caption,
        command=subprocess.list2cmdline(list(argv)) if os.name == "nt" else shlex.join(argv),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.returncode,
        elapsed_seconds=time.monotonic() - started,
    )
    steps.append(step)
    return step


def save_transcript(steps: list[CapturedStep]) -> None:
    path = REPOSITORY / "tmp" / "research" / "readme-demo" / "transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(step) for step in steps], indent=2) + "\n",
        encoding="utf-8",
    )


def validate_transitions(steps: list[CapturedStep], project: Path, run_root: Path) -> None:
    expected = [
        (0, "accepted: totals-behavior"),
        (0, "totals-behavior: PASS"),
        (0, "READY:"),
        (0, "Updated src/totals.py with an off-by-one."),
        (3, "BLOCKED:", "stale"),
        (1, "totals-behavior: FAIL", "expected 42, printed 45"),
        (1, "REJECTED:"),
        (0, "Restored src/totals.py."),
        (0, "totals-behavior: PASS"),
        (0, "READY:"),
    ]
    if len(steps) != len(expected):
        raise RuntimeError(f"captured {len(steps)} steps, expected {len(expected)}")
    for index, (exit_code, *markers) in enumerate(expected):
        step = steps[index]
        output = step.stdout + step.stderr
        if step.exit_code != exit_code or any(marker not in output for marker in markers):
            raise RuntimeError(
                f"unexpected step {index + 1} ({step.caption}): "
                f"exit={step.exit_code}, expected={exit_code}, output={output.strip()!r}"
            )

    manifest = json.loads((EXAMPLE / "verification" / "manifest.json").read_text(encoding="utf-8"))
    check = next(check for check in manifest["checks"] if check["id"] == "totals-behavior")
    scenario_ids = check["required_scenarios"]
    if len(scenario_ids) != 6:
        raise RuntimeError(f"totals-behavior now declares {len(scenario_ids)} scenarios, expected six")
    for index, expected_results in (
        (1, ["PASS"] * len(scenario_ids)),
        (5, ["PASS" if scenario == "empty-cart" else "FAIL" for scenario in scenario_ids]),
        (8, ["PASS"] * len(scenario_ids)),
    ):
        observed = re.findall(
            r"^\s+\[(PASS|FAIL)\] ([^:\s]+):",
            steps[index].stdout,
            flags=re.MULTILINE,
        )
        if [scenario for _, scenario in observed] != scenario_ids:
            raise RuntimeError(f"step {index + 1} reported unexpected scenarios: {observed!r}")
        if [result for result, _ in observed] != expected_results:
            raise RuntimeError(f"step {index + 1} reported unexpected scenario results: {observed!r}")
        results = [result for result, _ in observed]
        expected_counts = (1, 5) if index == 5 else (6, 0)
        if (results.count("PASS"), results.count("FAIL")) != expected_counts:
            raise RuntimeError(f"step {index + 1} reported unexpected PASS/FAIL counts: {observed!r}")

    for step in steps:
        output = step.stdout + step.stderr
        if str(run_root) in output or str(run_root).replace("\\", "/") in output:
            raise RuntimeError("captured CLI output contains the private demo path")
    if (project / "src" / "totals.py").read_bytes() != (EXAMPLE / "src" / "totals.py").read_bytes():
        raise RuntimeError("the copied totals source was not restored")


EXAMPLE = REPOSITORY / "examples" / "python-cli"


def python_edit(source: str, replacement: str, message: str) -> list[str]:
    code = (
        "from pathlib import Path; "
        "p=Path('src/totals.py'); s=p.read_text(encoding='utf-8'); "
        f"old={source!r}; new={replacement!r}; "
        "assert old in s; p.write_text(s.replace(old, new, 1), encoding='utf-8'); "
        f"print({message!r})"
    )
    return ["python", "-c", code]


def record() -> list[CapturedStep]:
    sys.path.insert(0, str(REPOSITORY))
    from examples.quickstart import prepare_demo

    project, environment = prepare_demo()
    run_root = project.parent
    source = project / "src" / "totals.py"
    original = source.read_bytes()
    steps: list[CapturedStep] = []
    commands = [
        ("Accept the copied registered scenario", ["vkit", "accept", "--project", ".", "--check", "totals-behavior", "--yes"]),
        ("Initial six-scenario check passes", ["vkit", "check", "run", "--project", ".", "--check", "totals-behavior"]),
        ("Gate reports fresh evidence", ["vkit", "gate", "--project", "."]),
    ]

    try:
        for caption, argv in commands:
            capture(steps, caption, argv, project, environment)

        capture(
            steps,
            "Introduce an off-by-one in the copied source",
            python_edit("        running += amount\n", "        running += amount + 1\n", "Updated src/totals.py with an off-by-one."),
            project,
            environment,
        )
        capture(steps, "After the source edit, gate blocks stale evidence", ["vkit", "gate", "--project", "."], project, environment)
        capture(
            steps,
            "Rerun after the edit finds incorrect totals",
            ["vkit", "check", "run", "--project", ".", "--check", "totals-behavior"],
            project,
            environment,
        )
        capture(steps, "Gate rejects the failing run", ["vkit", "gate", "--project", "."], project, environment)
        capture(
            steps,
            "Restore the copied source after the failure",
            python_edit("        running += amount + 1\n", "        running += amount\n", "Restored src/totals.py."),
            project,
            environment,
        )
        capture(
            steps,
            "After restoring the source, all six scenarios pass",
            ["vkit", "check", "run", "--project", ".", "--check", "totals-behavior"],
            project,
            environment,
        )
        capture(steps, "Fresh evidence is READY after the restore", ["vkit", "gate", "--project", "."], project, environment)
    finally:
        source.write_bytes(original)
        save_transcript(steps)

    validate_transitions(steps, project, run_root)
    return steps


def font(size: int):
    from PIL import ImageFont

    candidates = (
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


def wrapped_lines(text: str, font_obj, max_width: int, draw) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        words = raw_line.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if current and draw.textlength(candidate, font=font_obj) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def render_gif(steps: list[CapturedStep]) -> Path:
    from PIL import Image, ImageDraw

    width, height = 1060, 620
    output_path = REPOSITORY / "docs" / "assets" / "broken-change.gif"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mono = font(18)
    title_font = font(25)
    small = font(15)
    frames = []
    durations = []
    max_text_width = width - 120
    content_bottom = height - 75

    for index, step in enumerate(steps, start=1):
        frame = Image.new("RGB", (width, height), "#0b1020")
        draw = ImageDraw.Draw(frame)
        if draw.textlength(step.caption, font=title_font) > width - 250:
            raise RuntimeError(f"GIF caption exceeds the fixed frame at step {index}")
        draw.rectangle((0, 0, width, 7), fill="#a8c8ff")
        draw.text((42, 30), "VKIT  /  REGISTERED SCENARIO CHECK", font=small, fill="#a8c8ff")
        draw.text((42, 67), step.caption, font=title_font, fill="#a8c8ff")
        draw.text((width - 132, 37), f"{index:02d} / {len(steps):02d}", font=small, fill="#8e9bb5")

        panel = (38, 120, width - 38, height - 55)
        draw.rounded_rectangle(panel, radius=13, fill="#131c2e", outline="#26334a", width=2)
        x, y = 60, 143
        command = f"$ {step.command}"
        output = step.stdout.rstrip()
        if step.stderr.strip():
            output = f"{output}\n[stderr]\n{step.stderr.rstrip()}" if output else step.stderr.rstrip()

        for is_output, content in ((False, command), (True, output)):
            for line in wrapped_lines(content, mono, max_text_width, draw):
                if draw.textlength(line, font=mono) > max_text_width or y + 25 > content_bottom:
                    raise RuntimeError(f"GIF text exceeds the fixed frame at step {index}")
                color = "#dce5f7"
                if is_output:
                    if "FAIL" in line or "REJECTED" in line:
                        color = "#ffb4ab"
                    elif "BLOCKED" in line or "stale" in line.lower():
                        color = "#fdd663"
                    elif "PASS" in line or "READY" in line:
                        color = "#a8dab5"
                draw.text((x, y), line, font=mono, fill=color)
                y += 25
            if not is_output:
                y += 11

        draw.text(
            (42, height - 35),
            f"exit {step.exit_code}  {step.elapsed_seconds:.2f}s",
            font=small,
            fill="#92a0ba",
        )
        frames.append(frame.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
        durations.append(2500 if "FAIL" not in output else 3000)

    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return output_path


def main() -> int:
    try:
        steps = record()
        gif = render_gif([*steps[:3], *steps[4:7], *steps[8:]])
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Recorded {len(steps)} CLI steps.")
    print(f"GIF: {gif.relative_to(REPOSITORY).as_posix()} ({gif.stat().st_size:,} bytes)")
    print("Transcript: tmp/research/readme-demo/transcript.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
