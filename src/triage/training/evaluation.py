from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_recall_fscore_support,
)
from sklearn.pipeline import Pipeline


def evaluate_model(model: Pipeline, texts: list[str], labels: list[str]) -> dict[str, Any]:
    predictions = model.predict(texts)
    probabilities = model.predict_proba(texts)
    classes = [str(value) for value in model.classes_]
    precision, recall, f1, support = precision_recall_fscore_support(
        labels, predictions, labels=classes, zero_division=0
    )
    per_class = {
        label: {
            "precision": float(precision[index]),
            "recall": float(recall[index]),
            "f1": float(f1[index]),
            "support": int(support[index]),
        }
        for index, label in enumerate(classes)
    }
    urgent_recall = per_class.get("urgente", {}).get("recall")
    macro_f1 = f1_score(
        labels,
        predictions,
        labels=classes,
        average="macro",
        zero_division=0,
    )
    return {
        "accuracy": float(accuracy_score(labels, predictions)),
        "macro_f1": float(macro_f1),
        "log_loss": float(log_loss(labels, probabilities, labels=classes)),
        "recall_urgente": None if urgent_recall is None else float(urgent_recall),
        "per_class": per_class,
        "confusion_matrix": confusion_matrix(labels, predictions, labels=classes).tolist(),
        "classes": classes,
        "calibration": _calibration_error(labels, probabilities, classes),
    }


def _calibration_error(labels: list[str], probabilities: np.ndarray, classes: list[str]) -> float:
    """Calcula ECE multiclasses sem depender de bibliotecas adicionais."""
    expected = np.asarray([classes.index(label) for label in labels])
    confidence = probabilities.max(axis=1)
    predicted = probabilities.argmax(axis=1)
    bins = np.linspace(0.0, 1.0, 11)
    error = 0.0
    for lower, upper in zip(bins[:-1], bins[1:], strict=True):
        selected = (confidence > lower) & (confidence <= upper)
        if selected.any():
            accuracy = float(np.mean(predicted[selected] == expected[selected]))
            error += float(selected.mean()) * abs(accuracy - float(confidence[selected].mean()))
    return error
