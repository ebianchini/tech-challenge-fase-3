from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from triage.training.dataset import DatasetValidationError, TextDataset


@dataclass(frozen=True)
class DatasetSplit:
    train_texts: list[str]
    test_texts: list[str]
    train_labels: list[str]
    test_labels: list[str]
    train_indices: list[int]
    test_indices: list[int]


def split_dataset(
    dataset: TextDataset,
    *,
    test_size: float = 0.2,
    random_state: int = 42,
    temporal: bool = False,
) -> DatasetSplit:
    if not 0 < test_size < 1:
        raise ValueError("test_size deve estar entre 0 e 1.")
    indices = list(range(len(dataset.texts)))
    if temporal:
        if not all(timestamp is not None for timestamp in dataset.timestamps):
            raise DatasetValidationError(
                "Split temporal exige timestamp preenchido em todas as linhas."
            )
        ordered = sorted(indices, key=lambda index: dataset.timestamps[index])  # type: ignore[arg-type]
        test_count = max(1, int(round(len(ordered) * test_size)))
        train_indices = ordered[:-test_count]
        test_indices = ordered[-test_count:]
    else:
        train_indices, test_indices = train_test_split(
            indices,
            test_size=test_size,
            random_state=random_state,
            stratify=dataset.labels,
        )
        train_indices.sort()
        test_indices.sort()
    if not train_indices or not test_indices:
        raise DatasetValidationError("Split gerou conjunto de treino ou teste vazio.")
    return DatasetSplit(
        train_texts=[dataset.texts[index] for index in train_indices],
        test_texts=[dataset.texts[index] for index in test_indices],
        train_labels=[dataset.labels[index] for index in train_indices],
        test_labels=[dataset.labels[index] for index in test_indices],
        train_indices=train_indices,
        test_indices=test_indices,
    )


def build_pipeline(
    *,
    min_df: int = 1,
    max_features: int | None = None,
    ngram_range: tuple[int, int] = (1, 2),
    max_iter: int = 1000,
    random_state: int = 42,
) -> Pipeline:
    if min_df < 1 or max_iter < 1:
        raise ValueError("min_df e max_iter devem ser positivos.")
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    lowercase=True,
                    ngram_range=ngram_range,
                    min_df=min_df,
                    max_features=max_features,
                ),
            ),
            (
                "classifier",
                LogisticRegression(
                    class_weight="balanced",
                    max_iter=max_iter,
                    random_state=random_state,
                ),
            ),
        ]
    )


def fit_pipeline(model: Pipeline, texts: Sequence[str], labels: Sequence[str]) -> Pipeline:
    if len(texts) != len(labels) or not texts:
        raise ValueError("textos e labels devem ter o mesmo tamanho e nao serem vazios.")
    return model.fit(texts, labels)
