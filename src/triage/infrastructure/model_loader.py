from __future__ import annotations

import json
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


class ONNXPredictor:
    """ONNX Runtime predictor for the exported text pipeline."""

    def __init__(self, model_path: Path, metadata_path: Path | None = None) -> None:
        if not model_path.is_file():
            raise ModelLoadError("Modelo ONNX indisponivel.")
        try:
            import onnxruntime as ort

            self._session = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            inputs = self._session.get_inputs()
            outputs = self._session.get_outputs()
            if len(inputs) != 1 or len(outputs) < 2:
                raise ValueError("Contrato ONNX inesperado.")
            self._input_name = inputs[0].name
            self._label_output = outputs[0].name
            self._probability_output = outputs[1].name
            self._classes = self._load_classes(metadata_path, outputs[0].name)
            self.model_version = self._load_model_version(metadata_path, model_path)
            self._warm_up()
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError("Falha ao carregar o artefato ONNX.") from exc

    def predict(self, texts: Sequence[str]) -> list[tuple[TriageClass, dict[TriageClass, float]]]:
        try:
            values = [[text] for text in texts]
            raw_outputs = self._session.run(
                [self._label_output, self._probability_output],
                {self._input_name: values},
            )
            labels = raw_outputs[0]
            probability_values = self._probability_rows(raw_outputs[1])
            results = []
            for label, row in zip(labels, probability_values, strict=True):
                predicted_class = TriageClass(str(label))
                probability_map = {
                    triage_class: float(row[index])
                    for index, triage_class in enumerate(self._classes)
                }
                results.append((predicted_class, probability_map))
            return results
        except Exception as exc:
            raise InferenceError("Falha durante a inferencia ONNX.") from exc

    def _warm_up(self) -> None:
        self.predict(["synthetic onnx warmup"])

    @staticmethod
    def _probability_rows(values: object) -> list[list[float]]:
        if hasattr(values, "tolist"):
            values = values.tolist()
        if not isinstance(values, list):
            raise ValueError("Saida de probabilidades ONNX invalida.")
        rows: list[list[float]] = []
        for row in values:
            if isinstance(row, dict):
                ordered = [row[key] for key in sorted(row)]
                rows.append([float(value) for value in ordered])
            elif isinstance(row, list):
                rows.append([float(value) for value in row])
            else:
                raise ValueError("Linha de probabilidades ONNX invalida.")
        return rows

    @staticmethod
    def _load_classes(metadata_path: Path | None, output_name: str) -> tuple[TriageClass, ...]:
        if metadata_path and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            classes = metadata.get("classes")
            if isinstance(classes, list):
                return tuple(TriageClass(value) for value in classes)
        if "classes" in output_name:
            return tuple(TriageClass(value) for value in ("atencao", "normal", "urgente"))
        raise ModelLoadError("Metadados de classes ONNX indisponiveis.")

    @staticmethod
    def _load_model_version(metadata_path: Path | None, model_path: Path) -> str:
        if metadata_path and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            version = metadata.get("model_version") or metadata.get("artifact_fingerprint")
            if isinstance(version, str) and version:
                return version
        return model_path.stem