from __future__ import annotations

import json
from pathlib import Path

import joblib
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import StringTensorType
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

ROOT_DIR = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT_DIR / "models"


def main() -> None:
    # Corpus artificial para validar o caminho tecnico; nao representa evidencia clinica.
    examples = [
        "synthetic baseline stable alpha",
        "synthetic baseline routine beta",
        "synthetic stable normal gamma",
        "synthetic review attention delta",
        "synthetic review attention epsilon",
        "synthetic signal attention zeta",
        "synthetic priority urgent eta",
        "synthetic priority urgent theta",
        "synthetic escalation urgent iota",
    ]
    labels = [
        "normal",
        "normal",
        "normal",
        "atencao",
        "atencao",
        "atencao",
        "urgente",
        "urgente",
        "urgente",
    ]

    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    features = vectorizer.fit_transform(examples)
    model = LogisticRegression(max_iter=500, random_state=42).fit(features, labels)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODELS_DIR / "model.joblib")
    joblib.dump(vectorizer, MODELS_DIR / "preprocessor.joblib")
    pipeline = Pipeline([("tfidf", vectorizer), ("classifier", model)])
    onnx_model = convert_sklearn(
        pipeline,
        initial_types=[("text", StringTensorType([None, 1]))],
        target_opset=15,
    )
    (MODELS_DIR / "triage.onnx").write_bytes(onnx_model.SerializeToString())
    metadata = {
        "model_version": "technical-demo-onnx-v1",
        "classes": ["atencao", "normal", "urgente"],
        "onnx_input_name": onnx_model.graph.input[0].name,
        "onnx_output_names": [value.name for value in onnx_model.graph.output],
        "onnx_probability_tolerance": 1e-5,
    }
    (MODELS_DIR / "model_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()