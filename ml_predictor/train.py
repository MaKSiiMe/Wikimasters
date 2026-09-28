#!/usr/bin/env python3
"""Train the WikiMasters trend model on a local GPU and export it to ONNX.

Meant to run on a workstation with a dedicated GPU (tested target: RTX 3060,
CUDA), NOT on the server. It pulls the daily traffic history collected by the
tracker from Redis, trains a small multi-task LSTM and writes:

    <out>/model.onnx   the network (CPU-friendly, dynamic batch)
    <out>/model.json   metadata / contract read by inference_api.py
    <out>/model.pt     PyTorch checkpoint (to resume or fine-tune)

Heads:
    trend       expected log-ratio of the next 7 days vs the last 7 days
    spike_prob  probability of a +300 % day within the next 7 days

Examples:
    # Redis of the server, reached through an SSH tunnel (see README)
    python train.py --redis-url redis://localhost:6379/0

    # smoke test without any data
    python train.py --synthetic 300 --epochs 5
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch
from torch import nn

import features as F

log = logging.getLogger("train")

DAILY_PREFIX = "wm:views:daily:"


# ----------------------------------------------------------------------------
#  Data
# ----------------------------------------------------------------------------

def load_series_from_redis(url: str, min_days: int) -> dict[str, tuple[np.ndarray, date]]:
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    client.ping()
    titles = sorted(client.smembers("wm:articles"))
    if not titles:  # tracker registry missing: fall back to scanning keys
        titles = sorted(k[len(DAILY_PREFIX):] for k in client.scan_iter(f"{DAILY_PREFIX}*", count=500))
    pipe = client.pipeline()
    for title in titles:
        pipe.hgetall(f"{DAILY_PREFIX}{title}")
    series = {}
    for title, raw in zip(titles, pipe.execute()):
        if not raw:
            continue
        views, end = F.daily_array(raw)
        if len(views) >= min_days:
            series[title] = (views, end)
    log.info("Loaded %d/%d articles with >= %d days of history from Redis", len(series), len(titles), min_days)
    return series


def synthetic_series(count: int, days: int, seed: int) -> dict[str, tuple[np.ndarray, date]]:
    """Wikipedia-like series: weekly seasonality, drift, and spikes that are
    often (not always) announced by a quiet multi-day ramp."""
    rng = np.random.default_rng(seed)
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    t = np.arange(days)
    out = {}
    for i in range(count):
        level = rng.lognormal(mean=6.0, sigma=1.4)
        weekly = 1.0 + rng.uniform(0.05, 0.25) * np.sin(2 * np.pi * t / 7 + rng.uniform(0, 2 * np.pi))
        drift = np.exp(np.cumsum(rng.normal(0.0, 0.02, days)))
        mult = np.ones(days)
        for _ in range(rng.poisson(days / 90)):
            pos = int(rng.integers(10, days))
            if rng.random() < 0.7:  # "frémissement" before the explosion
                ramp = int(rng.integers(3, 8))
                start = max(pos - ramp, 0)
                mult[start:pos] *= np.geomspace(1.05, rng.uniform(1.4, 2.5), pos - start)
            magnitude = rng.uniform(4.0, 25.0)
            half_life = rng.uniform(0.7, 4.0)
            decay = magnitude ** (0.5 ** ((t[pos:] - pos) / half_life))
            mult[pos:] *= decay
        views = rng.poisson(np.maximum(level * weekly * drift * mult, 0.0)).astype(np.float64)
        out[f"synthetic_{i:04d}"] = (views, end)
    log.info("Generated %d synthetic series of %d days", count, days)
    return out


@dataclass
class Dataset:
    x: np.ndarray
    trend: np.ndarray
    spike: np.ndarray

    def __len__(self) -> int:
        return len(self.x)


def make_datasets(series: dict[str, tuple[np.ndarray, date]], val_frac: float, stride: int
                  ) -> tuple[Dataset, Dataset]:
    """Temporal split per article: the last ``val_frac`` of each timeline is
    validation, and training windows never see a validation day (no leakage)."""
    parts = {"train": ([], [], []), "val": ([], [], [])}
    for views, end in series.values():
        x, y_trend, y_spike, t = F.build_windows(views, end, stride=stride)
        if not len(x):
            continue
        cut = int(len(views) * (1.0 - val_frac))
        masks = {"train": t + F.HORIZON_DAYS < cut, "val": t >= cut}
        for name, mask in masks.items():
            for bucket, arr in zip(parts[name], (x, y_trend, y_spike)):
                bucket.append(arr[mask])

    def concat(name: str) -> Dataset:
        xs, ts, ss = parts[name]
        if not xs:
            raise SystemExit(f"No {name} windows: collect more history (>= {F.WINDOW_DAYS + F.HORIZON_DAYS} days)")
        return Dataset(np.concatenate(xs), np.concatenate(ts), np.concatenate(ss))

    return concat("train"), concat("val")


# ----------------------------------------------------------------------------
#  Model
# ----------------------------------------------------------------------------

class TrendNet(nn.Module):
    """Small 2-layer LSTM with a shared trunk and two heads (~30k params)."""

    def __init__(self, n_features: int = F.N_FEATURES, hidden: int = 48, layers: int = 2, dropout: float = 0.1):
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden, num_layers=layers, batch_first=True,
                            dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.Linear(hidden, hidden), nn.ReLU(), nn.Dropout(dropout), nn.Linear(hidden, 2))

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        out, _ = self.lstm(x)
        y = self.head(out[:, -1])
        return y[:, 0], y[:, 1]  # trend, spike logit


class ExportWrapper(nn.Module):
    """What ships to ONNX: sigmoid applied so the server gets a probability."""

    def __init__(self, model: TrendNet):
        super().__init__()
        self.model = model

    def forward(self, series: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        trend, logit = self.model(series)
        return trend, torch.sigmoid(logit)


# ----------------------------------------------------------------------------
#  Training
# ----------------------------------------------------------------------------

def pick_device(requested: str) -> torch.device:
    if requested == "auto":
        requested = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(requested)
    if device.type == "cuda":
        if not torch.cuda.is_available():
            raise SystemExit("CUDA requested but not available — check the NVIDIA driver / torch CUDA build")
        torch.backends.cudnn.benchmark = True
        # Ampere (RTX 30xx) : TF32 matmuls are much faster at no practical cost here.
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        props = torch.cuda.get_device_properties(device)
        log.info("Using GPU %s (%.1f GB VRAM, CUDA %s)", props.name, props.total_memory / 1e9, torch.version.cuda)
    else:
        log.warning("Training on CPU — fine for a smoke test, slow for real data")
    return device


def roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    pos = y > 0.5
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = np.empty(len(p))
    ranks[np.argsort(p, kind="mergesort")] = np.arange(1, len(p) + 1)
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


@torch.no_grad()
def evaluate(model: TrendNet, data: dict[str, torch.Tensor], batch_size: int, spike_loss: nn.Module,
             spike_weight: float) -> dict[str, float]:
    model.eval()
    trends, probs, losses = [], [], []
    n = len(data["x"])
    for i in range(0, n, batch_size):
        x = data["x"][i:i + batch_size]
        trend, logit = model(x)
        loss = nn.functional.smooth_l1_loss(trend, data["trend"][i:i + batch_size], reduction="sum") \
            + spike_weight * spike_loss(logit, data["spike"][i:i + batch_size]) * len(x)
        losses.append(loss.item())
        trends.append(trend.float().cpu().numpy())
        probs.append(torch.sigmoid(logit).float().cpu().numpy())
    pred_t, pred_p = np.concatenate(trends), np.concatenate(probs)
    y_t, y_s = data["trend"].cpu().numpy(), data["spike"].cpu().numpy()
    moving = np.abs(y_t) > 0.05
    predicted = pred_p >= 0.5
    tp = int((predicted & (y_s > 0.5)).sum())
    return {
        "loss": sum(losses) / n,
        "trend_mae": float(np.mean(np.abs(pred_t - y_t))),
        "direction_acc": float(np.mean(np.sign(pred_t[moving]) == np.sign(y_t[moving]))) if moving.any() else float("nan"),
        "spike_auc": roc_auc(y_s, pred_p),
        "spike_precision": tp / max(int(predicted.sum()), 1),
        "spike_recall": tp / max(int((y_s > 0.5).sum()), 1),
    }


def train(args: argparse.Namespace, train_ds: Dataset, val_ds: Dataset, device: torch.device
          ) -> tuple[TrendNet, dict[str, float], int]:
    def to_device(ds: Dataset) -> dict[str, torch.Tensor]:
        # The whole dataset fits in VRAM (tens of MB): no DataLoader overhead.
        return {k: torch.from_numpy(v).to(device) for k, v in (("x", ds.x), ("trend", ds.trend), ("spike", ds.spike))}

    tr, va = to_device(train_ds), to_device(val_ds)
    positives = float(train_ds.spike.sum())
    pos_weight = min(max((len(train_ds) - positives) / max(positives, 1.0), 1.0), 50.0)
    spike_loss = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight, device=device))
    log.info("Train windows: %d (%.2f %% spikes, pos_weight=%.1f) — val windows: %d",
             len(train_ds), 100 * positives / len(train_ds), pos_weight, len(val_ds))

    model = TrendNet(hidden=args.hidden, layers=args.layers, dropout=args.dropout).to(device)
    log.info("TrendNet: %d parameters", sum(p.numel() for p in model.parameters()))
    optim = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, factor=0.5, patience=3)

    best_loss, best_state, best_metrics, best_epoch, stale = math.inf, None, {}, 0, 0
    n = len(train_ds)
    for epoch in range(1, args.epochs + 1):
        model.train()
        started, total = time.monotonic(), 0.0
        order = torch.randperm(n, device=device)
        for i in range(0, n, args.batch_size):
            idx = order[i:i + args.batch_size]
            trend, logit = model(tr["x"][idx])
            loss = nn.functional.smooth_l1_loss(trend, tr["trend"][idx]) \
                + args.spike_weight * spike_loss(logit, tr["spike"][idx])
            optim.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optim.step()
            total += loss.item() * len(idx)

        metrics = evaluate(model, va, args.batch_size * 4, spike_loss, args.spike_weight)
        sched.step(metrics["loss"])
        log.info("epoch %3d | train %.4f | val %.4f | MAE %.3f | dir %.1f %% | spike AUC %.3f P %.2f R %.2f | %.1fs",
                 epoch, total / n, metrics["loss"], metrics["trend_mae"], 100 * metrics["direction_acc"],
                 metrics["spike_auc"], metrics["spike_precision"], metrics["spike_recall"],
                 time.monotonic() - started)
        if metrics["loss"] < best_loss - 1e-4:
            best_loss, best_metrics, best_epoch, stale = metrics["loss"], metrics, epoch, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            stale += 1
            if stale >= args.patience:
                log.info("Early stopping (no improvement for %d epochs)", args.patience)
                break

    model.load_state_dict(best_state)
    return model, best_metrics, best_epoch


# ----------------------------------------------------------------------------
#  Export
# ----------------------------------------------------------------------------

def export_onnx(model: TrendNet, path: Path, opset: int) -> float:
    """Export to ONNX (dynamic batch) and check ONNX Runtime matches PyTorch."""
    import onnxruntime as ort

    wrapper = ExportWrapper(model.cpu().eval()).eval()
    dummy = torch.zeros(1, F.WINDOW_DAYS, F.N_FEATURES)  # batch 1 = safe LSTM export
    tmp = path.with_suffix(".onnx.tmp")
    with warnings.catch_warnings():
        # The torch.export-based exporter specialises the LSTM batch axis to the
        # dummy's size; the legacy TorchScript exporter keeps it dynamic.
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        # Emitted for any dynamic-batch LSTM; the ORT check below covers batch 1 and 16.
        warnings.filterwarnings("ignore", message="Exporting a model to ONNX with a batch_size other than 1")
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        torch.onnx.export(
            wrapper, (dummy,), str(tmp),
            input_names=["series"], output_names=["trend", "spike_prob"],
            dynamic_axes={"series": {0: "batch"}, "trend": {0: "batch"}, "spike_prob": {0: "batch"}},
            opset_version=opset,
            dynamo=False,
        )
    session = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"])
    max_diff = 0.0
    for batch in (1, 16):
        probe = torch.randn(batch, F.WINDOW_DAYS, F.N_FEATURES)
        with torch.no_grad():
            ref = [t.numpy() for t in wrapper(probe)]
        got = session.run(None, {"series": probe.numpy()})
        max_diff = max([max_diff] + [float(np.max(np.abs(a - b))) for a, b in zip(ref, got)])
    if max_diff > 1e-3:
        raise RuntimeError(f"ONNX output differs from PyTorch (max diff {max_diff:.2e})")
    os.replace(tmp, path)  # atomic: the inference API hot-reloads on mtime change
    return max_diff


def write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
#  CLI
# ----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("data")
    src.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    src.add_argument("--synthetic", type=int, default=0, metavar="N",
                     help="train on N synthetic series instead of Redis (smoke test)")
    src.add_argument("--synthetic-days", type=int, default=540)
    src.add_argument("--min-days", type=int, default=F.WINDOW_DAYS + F.HORIZON_DAYS + 30,
                     help="ignore articles with a shorter history")
    src.add_argument("--val-frac", type=float, default=0.2)
    src.add_argument("--stride", type=int, default=1, help="step between training windows (days)")
    hp = p.add_argument_group("training")
    hp.add_argument("--epochs", type=int, default=60)
    hp.add_argument("--batch-size", type=int, default=512)
    hp.add_argument("--lr", type=float, default=2e-3)
    hp.add_argument("--hidden", type=int, default=48)
    hp.add_argument("--layers", type=int, default=2)
    hp.add_argument("--dropout", type=float, default=0.1)
    hp.add_argument("--spike-weight", type=float, default=0.5, help="weight of the spike head loss")
    hp.add_argument("--patience", type=int, default=8)
    hp.add_argument("--device", default="auto", help="auto | cuda | cuda:0 | cpu")
    hp.add_argument("--seed", type=int, default=42)
    out = p.add_argument_group("output")
    out.add_argument("--out", type=Path, default=Path(__file__).with_name("models"))
    out.add_argument("--opset", type=int, default=17)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    args = parse_args(argv)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = pick_device(args.device)

    if args.synthetic:
        series = synthetic_series(args.synthetic, args.synthetic_days, args.seed)
        source = f"synthetic:{args.synthetic}"
    else:
        series = load_series_from_redis(args.redis_url, args.min_days)
        source = "redis"
    if not series:
        raise SystemExit("No usable series. Is the tracker running and the Redis tunnel open?")

    train_ds, val_ds = make_datasets(series, args.val_frac, args.stride)
    model, metrics, best_epoch = train(args, train_ds, val_ds, device)

    args.out.mkdir(parents=True, exist_ok=True)
    onnx_path = args.out / "model.onnx"
    max_diff = export_onnx(model, onnx_path, args.opset)
    version = datetime.now(timezone.utc).strftime("lstm-%Y%m%dT%H%M%SZ")
    torch.save({"state_dict": model.state_dict(), "hparams": {
        "hidden": args.hidden, "layers": args.layers, "dropout": args.dropout}, "version": version},
        args.out / "model.pt")
    write_json_atomic(args.out / "model.json", {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_version": F.FEATURE_VERSION,
        "features": list(F.FEATURE_NAMES),
        "window_days": F.WINDOW_DAYS,
        "horizon_days": F.HORIZON_DAYS,
        "baseline_days": F.BASELINE_DAYS,
        "spike_ratio": F.SPIKE_RATIO,
        "input": {"name": "series", "shape": ["batch", F.WINDOW_DAYS, F.N_FEATURES], "dtype": "float32"},
        "outputs": ["trend", "spike_prob"],
        "source": source,
        "articles": len(series),
        "train_windows": len(train_ds),
        "val_windows": len(val_ds),
        "best_epoch": best_epoch,
        "metrics": {k: round(v, 4) for k, v in metrics.items() if not math.isnan(v)},
        "onnx_max_abs_diff": max_diff,
        "hparams": {k: v for k, v in vars(args).items() if k in {
            "hidden", "layers", "dropout", "lr", "batch_size", "spike_weight", "epochs", "stride", "seed"}},
        "env": {"torch": torch.__version__, "cuda": torch.version.cuda, "device": str(device),
                "python": platform.python_version()},
    })
    size_kb = onnx_path.stat().st_size / 1024
    log.info("Exported %s (%.0f KB, ORT max diff %.1e) — version %s", onnx_path, size_kb, max_diff, version)
    log.info("Deploy: copy model.onnx + model.json to the server's MODEL_DIR_HOST (hot-reloaded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
