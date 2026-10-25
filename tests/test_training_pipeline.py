from __future__ import annotations

import csv
import json

import pytest

from triage.training.dataset import ClinicalTrainingBlocked, load_csv
from triage.training.train import train


def _write_dataset(path, *, label_column="label_original") -> None:
    rows = [
        ("alpha normal", "normal"),
        ("beta normal", "normal"),
        ("alpha attention", "atencao"),
        ("beta attention", "atencao"),
        ("alpha urgent", "urgente"),
        ("beta urgent", "urgente"),
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["abstract", label_column])
        writer.writerows(rows)


def test_loader_normalizes_text_and_keeps_original_label(tmp_path) -> None:
    source = tmp_path / "abstracts.csv"
    _write_dataset(source)

    dataset = load_csv(source)

    assert dataset.texts[0] == "alpha normal"
    assert dataset.label_column == "label_original"
    assert dataset.classes == ("atencao", "normal", "urgente")
    assert len(dataset.fingerprint) == 64


def test_loader_accepts_medical_abstracts_corpus_columns(tmp_path) -> None:
    source = tmp_path / "medical_tc.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["condition_label", "medical_abstract"])
        writer.writerows(
            [
                ("1", "first abstract"),
                ("1", "second abstract"),
                ("2", "third abstract"),
                ("2", "fourth abstract"),
            ]
        )

    dataset = load_csv(source)

    assert dataset.label_column == "condition_label"
    assert dataset.texts[0] == "first abstract"
    assert dataset.classes == ("1", "2")


def test_clinical_training_requires_approved_urgency_labels(tmp_path) -> None:
    source = tmp_path / "abstracts.csv"
    _write_dataset(source)

    with pytest.raises(ClinicalTrainingBlocked, match="urgency_label"):
        load_csv(source, clinical=True, clinical_approval=True)


def test_train_persists_pipeline_split_fingerprint_and_metrics(tmp_path) -> None:
    source = tmp_path / "abstracts.csv"
    output = tmp_path / "models"
    _write_dataset(source)

    metadata = train(source, output, test_size=0.5, random_state=42)
    saved = json.loads((output / "metadata.json").read_text(encoding="utf-8"))

    assert (output / "pipeline.joblib").is_file()
    assert (output / "split.json").is_file()
    assert saved["dataset_fingerprint"] == metadata["dataset_fingerprint"]
    assert set(saved["metrics"]["per_class"]) == {"normal", "atencao", "urgente"}
    assert "confusion_matrix" in saved["metrics"]
