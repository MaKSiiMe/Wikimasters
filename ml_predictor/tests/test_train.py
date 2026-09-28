import json

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("onnxruntime")

import features as F  # noqa: E402
import train  # noqa: E402


def test_temporal_split_has_no_leakage():
    series = train.synthetic_series(3, 200, seed=1)
    train_ds, val_ds = train.make_datasets(series, val_frac=0.25, stride=1)
    cut = int(200 * 0.75)
    # train windows: last day + horizon strictly before the cut; val after it
    assert len(train_ds) == 3 * (cut - F.HORIZON_DAYS - (F.WINDOW_DAYS - 1))
    assert len(val_ds) == 3 * (200 - F.HORIZON_DAYS - cut)


def test_roc_auc():
    y = np.array([0, 0, 1, 1])
    assert train.roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert train.roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert np.isnan(train.roc_auc(np.zeros(3), np.zeros(3)))


def test_end_to_end_synthetic_training(tmp_path):
    rc = train.main(["--synthetic", "12", "--synthetic-days", "120", "--epochs", "2",
                     "--device", "cpu", "--out", str(tmp_path)])
    assert rc == 0
    meta = json.loads((tmp_path / "model.json").read_text())
    assert meta["window_days"] == F.WINDOW_DAYS and meta["feature_version"] == F.FEATURE_VERSION
    assert meta["onnx_max_abs_diff"] < 1e-3
    assert (tmp_path / "model.onnx").stat().st_size > 10_000
    assert (tmp_path / "model.pt").exists()


def test_calibration_undoes_pos_weight_inflation():
    rng = np.random.default_rng(0)
    labels = (rng.random(4000) < 0.05).astype(np.float32)
    # logits shifted up by log(20), as a pos_weight of 20 would do
    logits = (labels * 3.0 - 1.5 + rng.normal(0, 1.0, 4000) + np.log(20.0)).astype(np.float32)
    a, b, method = train.fit_calibration(logits, labels, pos_weight=20.0)
    probs = 1 / (1 + np.exp(-(a * logits + b)))
    assert method == "platt" and a > 0
    assert abs(probs.mean() - labels.mean()) < 0.01  # calibrated on average
    assert train.fit_calibration(logits[:5], labels[:5], 20.0) == (1.0, -np.log(20.0), "prior")
