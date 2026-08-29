#!/usr/bin/env python3
"""Evaluate recorded FlowSpec output fixtures with deterministic assertions."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def evaluate(root: Path, cases_path: Path) -> dict[str, Any]:
    payload = load_json(cases_path)
    results: list[dict[str, Any]] = []
    total_assertions = 0
    passed_assertions = 0

    for case in payload.get("cases", []):
        case_id = str(case.get("id", "?"))
        baseline_path = resolve(root, str(case["baseline_output"]))
        with_skill_path = resolve(root, str(case["with_skill_output"]))
        baseline = baseline_path.read_text(encoding="utf-8")
        with_skill = with_skill_path.read_text(encoding="utf-8")
        assertions = case.get("assertions", {})
        checks: list[dict[str, Any]] = []

        for value in assertions.get("required_in_with_skill", []):
            checks.append(
                {
                    "kind": "required_in_with_skill",
                    "value": value,
                    "passed": value in with_skill,
                }
            )
        for value in assertions.get("forbidden_in_with_skill", []):
            checks.append(
                {
                    "kind": "forbidden_in_with_skill",
                    "value": value,
                    "passed": value not in with_skill,
                }
            )
        for value in assertions.get("expected_missing_from_baseline", []):
            checks.append(
                {
                    "kind": "expected_missing_from_baseline",
                    "value": value,
                    "passed": value not in baseline,
                }
            )
        for value, minimum in assertions.get("minimum_occurrences", {}).items():
            actual = with_skill.count(value)
            checks.append(
                {
                    "kind": "minimum_occurrences",
                    "value": value,
                    "minimum": minimum,
                    "actual": actual,
                    "passed": actual >= int(minimum),
                }
            )
        for value in assertions.get("required_drawio_artifacts", []):
            artifact_path = resolve(root, value)
            root_tag = None
            try:
                root_tag = ET.parse(artifact_path).getroot().tag
                passed = artifact_path.suffix.lower() == ".drawio" and root_tag == "mxfile"
            except (FileNotFoundError, ET.ParseError, OSError):
                passed = False
            checks.append(
                {
                    "kind": "required_drawio_artifact",
                    "value": value,
                    "root_tag": root_tag,
                    "passed": passed,
                }
            )
        for value in assertions.get("forbidden_artifact_globs", []):
            matches = sorted(str(path.relative_to(root)) for path in root.glob(value))
            checks.append(
                {
                    "kind": "forbidden_artifact_glob",
                    "value": value,
                    "matches": matches,
                    "passed": not matches,
                }
            )

        total_assertions += len(checks)
        passed_assertions += sum(check["passed"] for check in checks)
        results.append(
            {
                "id": case_id,
                "prompt": case.get("prompt"),
                "input_files": case.get("input_files", []),
                "baseline_output": str(baseline_path.relative_to(root)),
                "with_skill_output": str(with_skill_path.relative_to(root)),
                "passed": all(check["passed"] for check in checks),
                "checks": checks,
                "human_notes": case.get("human_notes", []),
            }
        )

    ok = bool(results) and all(result["passed"] for result in results)
    return {
        "ok": ok,
        "evidence_type": "recorded_fixture",
        "provider_backed": False,
        "human_blind_review": False,
        "summary": {
            "cases": len(results),
            "passed_cases": sum(result["passed"] for result in results),
            "assertions": total_assertions,
            "passed_assertions": passed_assertions,
        },
        "results": results,
        "limitations": [
            "Fixtures are deterministic regression evidence, not a live model run.",
            "The baseline is illustrative and was not produced in a controlled provider comparison.",
            "No human blind review or real product-team acceptance study has been completed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", nargs="?", default=".", help="FlowSpec skill directory")
    parser.add_argument("--cases", default="evals/output_cases.json")
    parser.add_argument("--output", "-o")
    args = parser.parse_args()

    root = Path(args.skill_dir).resolve()
    cases_path = resolve(root, args.cases)
    result = evaluate(root, cases_path)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = resolve(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
