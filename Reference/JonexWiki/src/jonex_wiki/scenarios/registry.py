"""Metadata discovery never imports a scenario processor."""

from __future__ import annotations
import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field


class ScenarioDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    version: str
    label: str
    description: str
    processor: str
    host_api: int = Field(default=1, ge=1, le=1)
    distribution_identity: str = "builtin"
    requires_model: bool = True
    kinds: list[str]
    kind_labels: dict[str, str] = Field(default_factory=dict)
    relations: list[str] = []
    instructions: str
    operations: list[str] = [
        "interpret",
        "materials",
        "material",
        "annotate",
        "resources",
        "review",
        "selection",
    ]
    config_schema: str = "wiki.scenario-processing.v1"
    strategies: list[str] = ["on_demand", "structured", "hybrid"]
    view: str = "resources"
    dependencies: list[str] = []
    input_schema: str = "wiki.material-units.v1"
    output_schema: str = "wiki.scenario-resources.v1"


class ScenarioRegistry:
    def __init__(self, descriptors=None):
        self._descriptors = {}
        if descriptors is None:
            descriptors = [
                json.loads(p.read_text())
                for p in sorted((Path(__file__).parent / "manifests").glob("*.json"))
            ]
            # Entry point value is a package containing scenario.json. Locate
            # its data without importing the processor or the package.
            for ep in entry_points(group="jonex_wiki.scenario_manifests"):
                package = ep.value.replace(".", "/")
                if not all(part.isidentifier() for part in package.split("/")):
                    raise ValueError("SCENARIO_MANIFEST_PACKAGE_INVALID")
                manifest = json.loads(
                    ep.dist.locate_file(package + "/scenario.json").read_text()
                )
                manifest["distribution_identity"] = (
                    ep.dist.metadata["Name"] + "==" + ep.dist.version
                )
                descriptors.append(manifest)
        for value in descriptors:
            self.register(value)

    def register(self, value):
        descriptor = ScenarioDescriptor.model_validate(value)
        if descriptor.id in self._descriptors:
            raise ValueError("SCENARIO_DUPLICATE_ID")
        self._descriptors[descriptor.id] = descriptor

    def list(self):
        return [d.model_dump() for d in self._descriptors.values()]

    def get(self, scenario_id):
        from jonex_wiki.kernel.errors import NotFoundError

        if scenario_id not in self._descriptors:
            raise NotFoundError("SCENARIO_NOT_REGISTERED")
        return self._descriptors[scenario_id]

    def load(self, scenario_id):
        descriptor = self.get(scenario_id)
        module, name = descriptor.processor.split(":")
        return getattr(importlib.import_module(module), name), descriptor
