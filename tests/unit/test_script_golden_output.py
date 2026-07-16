from __future__ import annotations

import hashlib
import re

from scripts.autonomous_researcher import main as autonomous_main
from scripts.combine_factors import main as combine_main
from scripts.research_director import main as director_main
from scripts.validate_strict import main as validate_main


def _golden_hash(stdout: str) -> str:
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
    normalized = re.sub(r"--validation-dir [^ ]+", "--validation-dir <TMP>", normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()


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
    assert _golden_hash(capsys.readouterr().out) == (
        "9e7beda5e3c0c128b6339878c6e714a74167bbbd8825ec3b548f5e61a5367ed1"
    )


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
    assert _golden_hash(capsys.readouterr().out) == (
        "d98295fa5c4b40d0b6630342db2c49bb3c0fc5f0ba0551692762f3fb351de150"
    )


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
    assert _golden_hash(capsys.readouterr().out) == (
        "b69d0606bdcf7bdd7d5e92c542093d8f5115190eeaf45b42707a706819a2187c"
    )


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
    assert _golden_hash(capsys.readouterr().out) == (
        "5d2ad7300a6f7a9940d56b4a1b410a9a60c98e370c86505fd2f5dc75c339de89"
    )
