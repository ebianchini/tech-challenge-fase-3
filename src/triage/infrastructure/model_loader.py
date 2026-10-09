from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import joblib

from triage.domain.exceptions import InferenceError, ModelUnavailableError
from triage.domain.models import TriageClass


class ModelLoadError(ModelUnavailableError):
    """Compatibilidade de nome para o adaptador de carregamento."""


class SklearnPredictor:
    def __init__(self, model_path: Path, preprocessor_path: Path) -> None:
        if not model_path.is_file() or not preprocessor_path.is_file():
            raise ModelLoadError("Modelo ou preprocessador indisponivel.")
        try:
            self._model = joblib.load(model_path)
            self._preprocessor = joblib.load(preprocessor_path)
        except Exception as exc:
            raise ModelLoadError("Falha ao carregar artefatos de inferencia.") from exc
        self.model_version = model_path.stem
        self._classes = tuple(TriageClass(value) for value in self._model.classes_)

    def predict(self, texts: Sequence[str]) -> list[tuple[TriageClass, dict[TriageClass, float]]]:
        try:
            features = self._preprocessor.transform(texts)
            probabilities = self._model.predict_proba(features)
        except Exception as exc:
            raise InferenceError("Falha durante a inferencia.") from exc

        results = []
        for row in probabilities:
            probability_map = {
                label: float(row[index]) for index, label in enumerate(self._classes)
            }
            predicted_class = max(probability_map, key=probability_map.__getitem__)
            results.append((predicted_class, probability_map))
        return results