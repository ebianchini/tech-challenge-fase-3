from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TriageClass(StrEnum):
    NORMAL = "normal"
    ATENCAO = "atencao"
    URGENTE = "urgente"


class TriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_text: str = Field(min_length=1, max_length=10_000)

    @field_validator("report_text")
    @classmethod
    def report_text_must_contain_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("report_text deve conter texto apos trim.")
        return value


class TriagePrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triage_class: TriageClass
    probabilities: Mapping[TriageClass, float]
    abstained: bool