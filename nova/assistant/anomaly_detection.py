"""Compatibilidad estable para Anomaly Detection.

v0.7.3 publicó el núcleo como `anomaly.py`, mientras algunos adaptadores ya
usaban `anomaly_detection`. Este módulo mantiene ambos nombres apuntando al
mismo núcleo para instalaciones actualizadas y referencias existentes.
"""

from .anomaly import AnomalyDetector, DEFAULT_ANOMALY_CONFIG, get_anomaly_detector

__all__ = ["AnomalyDetector", "DEFAULT_ANOMALY_CONFIG", "get_anomaly_detector"]
