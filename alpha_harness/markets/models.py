"""Typed configuration contracts for market-specific research packs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketDataConfig(BaseModel):
    """Data-loader configuration owned by a market pack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    loader: Literal["bigquery", "parquet", "synthetic"]
    project: str | None = None
    project_env: str | None = None
    dataset: str | None = None
    dataset_env: str | None = None
    base_path: str | None = None
    tables: dict[str, str] = Field(default_factory=dict)
    join_columns: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    loader_kwargs: dict[str, str | int | float | bool] = Field(default_factory=dict)


class MarketTopicConfig(BaseModel):
    """Static inputs from which Stage 2 will construct a research topic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic_id: str
    executor: str = "propose"
    theme: str
    priority: int
    rationale: str
    extra_guidance: str = ""
    validation_args: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    history_penalty: int = Field(default=0, ge=0)


class PostRunTransitions(BaseModel):
    """Market-owned transition data consumed by the generic Stage 2 policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    on_promotion: dict[str, str] = Field(default_factory=dict)
    on_data_gap: str | None = None
    stop_after_topics: tuple[str, ...] = ()


class MarketPack(BaseModel):
    """Versioned, immutable bundle of market-specific research knowledge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    market_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    universe_file: str
    data: MarketDataConfig
    extra_dsl_fields: dict[str, str] = Field(default_factory=dict)
    mock_presets: tuple[str, ...] = ()
    director_topics: tuple[MarketTopicConfig, ...] = ()
    post_run_transitions: PostRunTransitions = Field(default_factory=PostRunTransitions)
    sql_templates: dict[str, str] = Field(default_factory=dict)

    @property
    def dsl_fields(self) -> frozenset[str]:
        return frozenset(self.extra_dsl_fields)
