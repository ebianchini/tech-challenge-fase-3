from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class DatasetValidationError(ValueError):
    """Indica que o dataset nao atende ao contrato de treinamento."""


class ClinicalTrainingBlocked(DatasetValidationError):
    """Impede treino clinico sem rotulagem e aprovacao explicitas."""


@dataclass(frozen=True)
class TextDataset:
    texts: list[str]
    labels: list[str]
    timestamps: list[datetime | None]
    label_column: str
    fingerprint: str

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.labels)))


TEXT_ALIASES = (
    "text",
    "report_text",
    "abstract",
    "medical_abstract",
    "description",
    "content",
)
LABEL_ALIASES = ("label_original", "label", "category", "target", "condition_label")
TIMESTAMP_ALIASES = ("timestamp", "created_at", "date", "published_at")


def _find_column(headers: Iterable[str], aliases: Iterable[str]) -> str | None:
    by_normalized = {header.strip().lower(): header for header in headers}
    for alias in aliases:
        if alias in by_normalized:
            return by_normalized[alias]
    return None


def _parse_timestamp(value: str, row_number: int) -> datetime:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetValidationError(
            f"timestamp invalido na linha {row_number}; use ISO-8601."
        ) from exc


def compute_fingerprint(rows: Iterable[tuple[str, str, str]]) -> str:
    canonical = "\n".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"))
        for row in rows
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_csv(
    path: str | Path,
    *,
    min_samples_per_class: int = 2,
    required_label_column: str | None = None,
    clinical: bool = False,
    clinical_approval: bool = False,
) -> TextDataset:
    """Carrega o corpus sem inferir labels clinicos a partir de labels tematicos."""
    source = Path(path)
    if not source.is_file():
        raise DatasetValidationError(f"Dataset nao encontrado: {source}")

    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise DatasetValidationError("CSV sem cabecalho.")
        headers = [header.strip() for header in reader.fieldnames]
        text_column = _find_column(headers, TEXT_ALIASES)
        label_column = required_label_column or _find_column(headers, LABEL_ALIASES)
        timestamp_column = _find_column(headers, TIMESTAMP_ALIASES)

        if text_column is None:
            raise DatasetValidationError(
                "CSV deve conter uma coluna de texto (text, abstract ou report_text)."
            )
        if label_column is None:
            raise DatasetValidationError(
                "CSV deve conter label_original ou urgency_label explicitamente."
            )
        if clinical and label_column != "urgency_label":
            raise ClinicalTrainingBlocked(
                "Treino clinico bloqueado: urgency_label aprovado nao foi encontrado."
            )
        if clinical and not clinical_approval:
            raise ClinicalTrainingBlocked(
                "Treino clinico bloqueado: aprovacao clinica explicita ausente."
            )

        texts: list[str] = []
        labels: list[str] = []
        timestamps: list[datetime | None] = []
        fingerprint_rows: list[tuple[str, str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            text = (row.get(text_column) or "").strip()
            label = (row.get(label_column) or "").strip()
            if not text or not label:
                raise DatasetValidationError(
                    f"texto e label sao obrigatorios na linha {row_number}."
                )
            timestamp = None
            if timestamp_column:
                raw_timestamp = (row.get(timestamp_column) or "").strip()
                if not raw_timestamp:
                    raise DatasetValidationError(
                        f"timestamp nulo na linha {row_number}."
                    )
                timestamp = _parse_timestamp(raw_timestamp, row_number)
            texts.append(text)
            labels.append(label)
            timestamps.append(timestamp)
            fingerprint_rows.append(
                (text, label, timestamp.isoformat() if timestamp else "")
            )

    if not texts:
        raise DatasetValidationError("Dataset vazio.")
    counts = {label: labels.count(label) for label in set(labels)}
    underrepresented = {
        label: count for label, count in counts.items() if count < min_samples_per_class
    }
    if underrepresented:
        raise DatasetValidationError(
            f"classes abaixo do minimo de amostras: {underrepresented}."
        )
    return TextDataset(
        texts=texts,
        labels=labels,
        timestamps=timestamps,
        label_column=label_column,
        fingerprint=compute_fingerprint(fingerprint_rows),
    )


def normalize_csv(path: str | Path, output_path: str | Path) -> Path:
    """Escreve uma copia normalizada com as colunas text e label_original."""
    dataset = load_csv(path, min_samples_per_class=1)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["text", "label_original"])
    writer.writerows(zip(dataset.texts, dataset.labels, strict=True))
    destination.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return destination
