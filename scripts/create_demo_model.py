from __future__ import annotations

from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

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


if __name__ == "__main__":
    main()