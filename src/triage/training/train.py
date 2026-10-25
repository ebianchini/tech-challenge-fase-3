from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import joblib
import yaml

from triage.training.dataset import load_csv
from triage.training.evaluation import evaluate_model
from triage.training.pipeline import build_pipeline, fit_pipeline, split_dataset


def train(
    data_path: str | Path,
    output_dir: str | Path,
    *,
    test_size: float = 0.2,
    random_state: int = 42,
    temporal_split: bool = False,
    clinical: bool = False,
    clinical_approval: bool = False,
    min_df: int = 1,
    max_features: int | None = None,
    max_iter: int = 1000,
    min_macro_f1: float = 0.0,
    min_urgent_recall: float = 0.0,
    params_file: str | Path | None = None,
) -> dict[str, Any]:
    if params_file is not None:
        parameters = _read_params(params_file)
        test_size = float(parameters.get("test_size", test_size))
        random_state = int(parameters.get("random_state", random_state))
        temporal_split = bool(parameters.get("temporal_split", temporal_split))
        min_df = int(parameters.get("min_df", min_df))
        max_features = parameters.get("max_features", max_features)
        max_iter = int(parameters.get("max_iter", max_iter))
        min_macro_f1 = float(parameters.get("min_macro_f1", min_macro_f1))
        min_urgent_recall = float(parameters.get("min_urgent_recall", min_urgent_recall))
    dataset = load_csv(
        data_path,
        clinical=clinical,
        clinical_approval=clinical_approval,
        required_label_column="urgency_label" if clinical else None,
    )
    split = split_dataset(
        dataset,
        test_size=test_size,
        random_state=random_state,
        temporal=temporal_split,
    )
    model = fit_pipeline(
        build_pipeline(
            min_df=min_df,
            max_features=max_features,
            max_iter=max_iter,
            random_state=random_state,
        ),
        split.train_texts,
        split.train_labels,
    )
    metrics = evaluate_model(model, split.test_texts, split.test_labels)
    gate = quality_gate(
        metrics,
        min_macro_f1=min_macro_f1,
        min_urgent_recall=min_urgent_recall,
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output / "pipeline.joblib")
    joblib.dump(model.named_steps["classifier"], output / "model.joblib")
    joblib.dump(model.named_steps["tfidf"], output / "preprocessor.joblib")
    (output / "split.json").write_text(
        json.dumps(
            {
                "train_indices": split.train_indices,
                "test_indices": split.test_indices,
                "dataset_fingerprint": dataset.fingerprint,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    metadata = {
        "dataset_fingerprint": dataset.fingerprint,
        "label_column": dataset.label_column,
        "label_semantics": "clinical" if clinical else "technical-demo",
        "classes": list(dataset.classes),
        "parameters": {
            "test_size": test_size,
            "random_state": random_state,
            "temporal_split": temporal_split,
            "min_df": min_df,
            "max_features": max_features,
            "max_iter": max_iter,
        },
        "pipeline": "TfidfVectorizer -> LogisticRegression",
        "metrics": metrics,
        "quality_gate": gate,
    }
    metadata["artifact_fingerprint"] = _json_fingerprint(metadata)
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return metadata


def quality_gate(
    metrics: dict[str, Any],
    *,
    min_macro_f1: float,
    min_urgent_recall: float,
) -> dict[str, Any]:
    urgent_recall = metrics.get("recall_urgente")
    checks = {
        "macro_f1": float(metrics["macro_f1"]) >= min_macro_f1,
        "recall_urgente": urgent_recall is not None
        and float(urgent_recall) >= min_urgent_recall,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "min_macro_f1": min_macro_f1,
            "min_urgent_recall": min_urgent_recall,
        },
    }


def _json_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_params(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        values = yaml.safe_load(stream) or {}
    parameters = values.get("train")
    if not isinstance(parameters, dict):
        raise ValueError("params.yaml deve conter um mapa train.")
    return parameters


def main() -> None:
    parser = argparse.ArgumentParser(description="Treina o pipeline de texto da triagem.")
    parser.add_argument("--data", default="data/interim/medical_tc_train_normalized.csv")
    parser.add_argument("--output-dir", default="models/training")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--temporal-split", action="store_true")
    parser.add_argument("--clinical", action="store_true")
    parser.add_argument("--clinical-approval", action="store_true")
    parser.add_argument("--min-df", type=int, default=1)
    parser.add_argument("--max-features", type=int)
    parser.add_argument("--max-iter", type=int, default=1000)
    parser.add_argument("--min-macro-f1", type=float, default=0.0)
    parser.add_argument("--min-urgent-recall", type=float, default=0.0)
    parser.add_argument("--params-file")
    args = parser.parse_args()
    train(
        args.data,
        args.output_dir,
        test_size=args.test_size,
        random_state=args.random_state,
        temporal_split=args.temporal_split,
        clinical=args.clinical,
        clinical_approval=args.clinical_approval,
        min_df=args.min_df,
        max_features=args.max_features,
        max_iter=args.max_iter,
        min_macro_f1=args.min_macro_f1,
        min_urgent_recall=args.min_urgent_recall,
        params_file=args.params_file,
    )


if __name__ == "__main__":
    main()
