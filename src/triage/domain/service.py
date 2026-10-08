from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import TriageClass, TriagePrediction


class Predictor(Protocol):
    model_version: str

    def predict(
        self, texts: Sequence[str]
    ) -> Sequence[tuple[TriageClass, dict[TriageClass, float]]]: ...


@dataclass(frozen=True)
class TriageService:
    predictor: Predictor
    abstention_threshold: float = 0.55

    def classify(self, texts: Sequence[str]) -> list[TriagePrediction]:
        predictions = []
        for triage_class, probabilities in self.predictor.predict(texts):
            confidence = probabilities[triage_class]
            predictions.append(
                TriagePrediction(
                    triage_class=triage_class,
                    probabilities=probabilities,
                    abstained=confidence < self.abstention_threshold,
                )
            )
        return predictions