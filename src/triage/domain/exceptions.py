class InvalidInputError(ValueError):
    """Entrada que nao atende as regras do caso de uso."""


class ModelUnavailableError(RuntimeError):
    """Modelo ou preprocessador nao esta disponivel para inferencia."""


class InferenceError(RuntimeError):
    """Falha controlada durante a inferencia."""