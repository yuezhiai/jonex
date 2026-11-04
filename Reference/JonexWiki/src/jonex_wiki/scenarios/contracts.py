"""Stable handoff to business modules; no learner state belongs in the engine."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_ids: list[str] = Field(min_length=1, max_length=200)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_review_version: int = Field(ge=0, strict=True)


class ScenarioSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["wiki.scenario-selection.v1"] = "wiki.scenario-selection.v1"
    tenant_id: str
    kb_id: str
    base_revision: str
    scenario_id: str
    scenario_version: str
    result_revision: str
    review_version: int
    objects: list[dict]
    actor: str
    created_at: str
