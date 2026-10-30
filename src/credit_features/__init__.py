"""Shared feature logic used by BOTH the training pipeline and the inference container.

One implementation in one package is how train/serve skew is prevented.
"""

from .transform import (
    APPLICATION_FIELDS,
    FACET_COLUMNS,
    FEATURE_ORDER,
    HISTORY_FIELDS,
    LABEL,
    MONOTONE_CONSTRAINTS,
    REASON_CODES,
    transform,
    transform_frame,
)

__version__ = "0.1.0"

__all__ = [
    "APPLICATION_FIELDS",
    "FACET_COLUMNS",
    "FEATURE_ORDER",
    "HISTORY_FIELDS",
    "LABEL",
    "MONOTONE_CONSTRAINTS",
    "REASON_CODES",
    "transform",
    "transform_frame",
]
