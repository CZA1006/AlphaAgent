"""Tests for registry implementations — base protocol and domain registries."""


from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.factor import FactorRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.registries.skill import SkillRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry
from alpha_harness.schemas.skill import Skill

# ── Base InMemoryRegistry ────────────────────────────────────────────────────


def test_in_memory_save_and_get():
    reg: InMemoryRegistry[Hypothesis] = InMemoryRegistry()
    h = Hypothesis(text="test hypothesis")
    saved_id = reg.save(h)
    assert saved_id == h.id
    retrieved = reg.get(h.id)
    assert retrieved is not None
    assert retrieved.text == "test hypothesis"


def test_in_memory_get_missing():
    reg: InMemoryRegistry[Hypothesis] = InMemoryRegistry()
    assert reg.get("nonexistent") is None


def test_in_memory_list_all():
    reg: InMemoryRegistry[Hypothesis] = InMemoryRegistry()
    h1 = Hypothesis(text="first")
    h2 = Hypothesis(text="second")
    reg.save(h1)
    reg.save(h2)
    assert len(reg.list_all()) == 2


def test_in_memory_search():
    reg: InMemoryRegistry[Hypothesis] = InMemoryRegistry()
    h1 = Hypothesis(text="momentum", status=HypothesisStatus.DRAFT)
    h2 = Hypothesis(text="reversion", status=HypothesisStatus.TESTING)
    reg.save(h1)
    reg.save(h2)
    results = reg.search(status="testing")
    assert len(results) == 1
    assert results[0].text == "reversion"


def test_in_memory_overwrite():
    reg: InMemoryRegistry[Hypothesis] = InMemoryRegistry()
    h = Hypothesis(text="original")
    reg.save(h)
    h_updated = h.model_copy(update={"text": "updated"})
    reg.save(h_updated)
    assert reg.get(h.id) is not None
    assert reg.get(h.id).text == "updated"  # type: ignore[union-attr]
    assert len(reg.list_all()) == 1


# ── ExperimentRegistry ───────────────────────────────────────────────────────


def _make_experiment(
    decision: ExperimentDecision = ExperimentDecision.ARCHIVE_ONLY,
    hypothesis_text: str = "test",
) -> ExperimentRecord:
    h = Hypothesis(text=hypothesis_text)
    f = FactorSpec(name="f", expression="close", hypothesis_id=h.id)
    ev = EvaluationBundle(ic=0.05 if decision != ExperimentDecision.REJECT else 0.01)
    return ExperimentRecord(hypothesis=h, factor=f, evaluation=ev, decision=decision)


def test_experiment_registry_list_by_decision():
    reg = ExperimentRegistry()
    reg.save(_make_experiment(ExperimentDecision.REJECT))
    reg.save(_make_experiment(ExperimentDecision.PROMOTE_CANDIDATE))
    reg.save(_make_experiment(ExperimentDecision.REJECT))
    assert len(reg.list_rejected()) == 2
    assert len(reg.list_promoted()) == 1


def test_experiment_registry_list_by_hypothesis():
    reg = ExperimentRegistry()
    e1 = _make_experiment(hypothesis_text="alpha")
    e2 = _make_experiment(hypothesis_text="beta")
    reg.save(e1)
    reg.save(e2)
    results = reg.list_by_hypothesis(e1.hypothesis.id)
    assert len(results) == 1
    assert results[0].hypothesis.text == "alpha"


# ── HypothesisRegistry ──────────────────────────────────────────────────────


def test_hypothesis_registry_list_by_status():
    reg = HypothesisRegistry()
    reg.save(Hypothesis(text="draft one"))
    reg.save(Hypothesis(text="testing", status=HypothesisStatus.TESTING))
    reg.save(Hypothesis(text="draft two"))
    assert len(reg.list_actionable()) == 2
    assert len(reg.list_by_status(HypothesisStatus.TESTING)) == 1


# ── FactorRegistry ───────────────────────────────────────────────────────────


def test_factor_registry_list_by_universe():
    reg = FactorRegistry()
    reg.save(FactorSpec(name="f1", expression="close", universe_id="u1"))
    reg.save(FactorSpec(name="f2", expression="volume", universe_id="u2"))
    reg.save(FactorSpec(name="f3", expression="high", universe_id="u1"))
    assert len(reg.list_by_universe("u1")) == 2
    assert len(reg.list_by_universe("u2")) == 1


# ── SkillRegistry ────────────────────────────────────────────────────────────


def test_skill_registry_list_promoted():
    reg = SkillRegistry()
    reg.save(Skill(name="s1", description="a", promoted=True, tags=["momentum"]))
    reg.save(Skill(name="s2", description="b", promoted=False, tags=["reversion"]))
    assert len(reg.list_promoted()) == 1
    assert len(reg.list_by_tag("momentum")) == 1
    assert len(reg.list_by_tag("value")) == 0


# ── MemoryRegistry ───────────────────────────────────────────────────────────


def test_memory_registry_list_by_category():
    reg = MemoryRegistry()
    reg.save(MemoryEntry(
        category=MemoryCategory.SUCCESS_PATTERN,
        content="momentum works in low-vol regimes",
    ))
    reg.save(MemoryEntry(
        category=MemoryCategory.FAILURE_PATTERN,
        content="reversion fails in trending markets",
    ))
    reg.save(MemoryEntry(
        category=MemoryCategory.SUCCESS_PATTERN,
        content="quality factor stable across subperiods",
    ))
    assert len(reg.list_by_category(MemoryCategory.SUCCESS_PATTERN)) == 2
    assert len(reg.list_by_category(MemoryCategory.FAILURE_PATTERN)) == 1


def test_memory_registry_list_by_experiment():
    reg = MemoryRegistry()
    reg.save(MemoryEntry(
        category=MemoryCategory.EXPERIMENT_LINEAGE,
        content="derived from exp_001",
        source_experiment_ids=["exp_001", "exp_002"],
    ))
    reg.save(MemoryEntry(
        category=MemoryCategory.META_POLICY,
        content="unrelated note",
    ))
    assert len(reg.list_by_experiment("exp_001")) == 1
    assert len(reg.list_by_experiment("exp_999")) == 0


def test_memory_registry_list_by_tag():
    reg = MemoryRegistry()
    reg.save(MemoryEntry(
        category=MemoryCategory.SUCCESS_PATTERN,
        content="tagged",
        tags=["momentum", "us_equity"],
    ))
    assert len(reg.list_by_tag("momentum")) == 1
    assert len(reg.list_by_tag("crypto")) == 0
