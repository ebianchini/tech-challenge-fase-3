from __future__ import annotations

from triage.domain.models import TriageClass
from triage.domain.service import TriageService


class FakePredictor:
    model_version = "test-model"

    def predict(self, texts):
        assert list(texts) == ["synthetic signal"]
        return [
            (
                TriageClass.ATENCAO,
                {
                    TriageClass.NORMAL: 0.1,
                    TriageClass.ATENCAO: 0.8,
                    TriageClass.URGENTE: 0.1,
                },
            )
        ]


class OrderedFakePredictor:
    model_version = "test-model"

    def predict(self, texts):
        assert list(texts) == ["first", "second"]
        return [
            (TriageClass.NORMAL, {TriageClass.NORMAL: 0.9}),
            (TriageClass.URGENTE, {TriageClass.URGENTE: 0.9}),
        ]


def test_service_classifies_and_does_not_depend_on_fastapi() -> None:
    result = TriageService(FakePredictor()).classify(["synthetic signal"])[0]

    assert result.triage_class == TriageClass.ATENCAO
    assert result.abstained is False


def test_service_preserves_prediction_order() -> None:
    results = TriageService(OrderedFakePredictor()).classify(["first", "second"])

    assert [result.triage_class for result in results] == [
        TriageClass.NORMAL,
        TriageClass.URGENTE,
    ]