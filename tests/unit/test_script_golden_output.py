from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts.autonomous_researcher import main as autonomous_main
from scripts.combine_factors import main as combine_main
from scripts.research_director import main as director_main
from scripts.validate_strict import main as validate_main

_GOLDEN_HASHES = json.loads(
    (Path(__file__).parents[1] / "golden" / "script_output_hashes.json").read_text(encoding="utf-8")
)
_FLOAT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:[0-9]+\.[0-9]+|[0-9]+\.(?![0-9])|\.[0-9]+)"
    r"(?:[eE][-+]?[0-9]+)?"
)


def _normalize_golden_output(stdout: str) -> str:
    normalized = re.sub(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z",
        "<TIMESTAMP>",
        stdout,
    )
    normalized = re.sub(
        r'"factor_id": "[0-9a-f]{12}"',
        '"factor_id": "<ID>"',
        normalized,
    )
    normalized = re.sub(r"\S*\.venv/bin/python3", "<PYTHON>", normalized)
    normalized = re.sub(r"--validation-dir [^ ]+", "--validation-dir <TMP>", normalized)
    return _FLOAT_TOKEN.sub(
        lambda match: format(float(match.group()), ".10g"),
        normalized,
    )


def _golden_hash(stdout: str) -> str:
    normalized = _normalize_golden_output(stdout)
    return hashlib.sha256(normalized.encode()).hexdigest()


def test_golden_float_normalization_accepts_ulp_only_difference() -> None:
    left = '{"ic": 0.123456789012345}'
    right = '{"ic": 0.123456789012346}'

    assert _normalize_golden_output(left) == _normalize_golden_output(right)


def test_golden_float_normalization_rejects_sixth_significant_digit_change() -> None:
    left = '{"ic": 0.123456789012345}'
    right = '{"ic": 0.123457789012345}'

    assert _normalize_golden_output(left) != _normalize_golden_output(right)


def test_validate_strict_golden_output(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_LLM_LOG_DIR", str(tmp_path / "llm"))
    rc = validate_main(
        [
            "--data-source",
            "synthetic",
            "--n-days",
            "240",
            "--n-symbols",
            "8",
            "--seed",
            "7",
            "--llm",
            "mock",
            "--n-candidates",
            "2",
            "--cycle-id",
            "golden-validation",
            "--no-memory",
            "--no-write",
            "--json",
        ]
    )

    assert rc == 0
    assert _golden_hash(capsys.readouterr().out) == _GOLDEN_HASHES["validate_strict"]


def test_combine_factors_golden_output(capsys) -> None:
    rc = combine_main(
        [
            "--data-source",
            "synthetic",
            "--n-days",
            "240",
            "--n-symbols",
            "8",
            "--seed",
            "7",
            "--expr",
            "rank(ts_mean(close, 20))",
            "--expr",
            "zscore(ts_delta(close, 5))",
            "--cycle-id",
            "golden-combine",
            "--no-write",
            "--json",
        ]
    )

    assert rc == 0
    assert _golden_hash(capsys.readouterr().out) == _GOLDEN_HASHES["combine_factors"]


def test_research_director_golden_output(capsys, tmp_path) -> None:
    rc = director_main(
        [
            "--market",
            "us_equities_daily",
            "--validation-dir",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert _golden_hash(capsys.readouterr().out) == _GOLDEN_HASHES["research_director"]


def test_autonomous_researcher_golden_output(capsys, tmp_path) -> None:
    rc = autonomous_main(
        [
            "--market",
            "us_equities_daily",
            "--run-id",
            "golden-autonomous",
            "--validation-dir",
            str(tmp_path),
            "--no-artifact",
        ]
    )

    assert rc == 0
    assert _golden_hash(capsys.readouterr().out) == _GOLDEN_HASHES["autonomous_researcher"]
