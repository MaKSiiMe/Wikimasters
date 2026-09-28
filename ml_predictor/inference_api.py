"""WikiMasters trend inference micro-service (CPU only).

Loads the ONNX model exported by train.py and scores articles from the daily
history the tracker keeps in Redis. Built for a very old server: ONNX Runtime
on the CPU execution provider, a single thread by default, no PyTorch.

Endpoints
    GET  /health                    liveness + loaded model version
    GET  /predict?article=<name>    trend score of an article tracked in Redis
    POST /predict                   {"article": "..."} or {"series": [..], "end_date": "YYYY-MM-DD"}

The model is hot-reloaded when model.onnx changes on disk. Without a model the
service still answers, using a momentum heuristic (``"model": "heuristic"``).

Run:  uvicorn inference_api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort
import redis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import features as F

__version__ = "0.1.0"

log = logging.getLogger("inference")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

MODEL_DIR = Path(os.getenv("MODEL_DIR", Path(__file__).with_name("models")))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ORT_NUM_THREADS = int(os.getenv("ORT_NUM_THREADS", "1"))
RELOAD_CHECK_SECONDS = 5.0


class ModelHolder:
    """Owns the ONNX session; reloads it when model.onnx is replaced."""

    def __init__(self, model_dir: Path):
        self.onnx_path = model_dir / "model.onnx"
        self.meta_path = model_dir / "model.json"
        self.session: ort.InferenceSession | None = None
        self.meta: dict = {}
        self._mtime: float | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()

    @property
    def version(self) -> str:
        return self.meta.get("version", "unknown") if self.session else "heuristic"

    def get(self) -> ort.InferenceSession | None:
        now = time.monotonic()
        if now - self._checked_at >= RELOAD_CHECK_SECONDS:
            with self._lock:
                self._checked_at = now
                self._reload_if_changed()
        return self.session

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.onnx_path.stat().st_mtime
        except FileNotFoundError:
            if self.session is not None:
                log.warning("Model file disappeared, falling back to heuristic")
            self.session, self.meta, self._mtime = None, {}, None
            return
        if mtime == self._mtime:
            return
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}
            if meta.get("feature_version", F.FEATURE_VERSION) != F.FEATURE_VERSION:
                raise ValueError(f"model built for feature_version={meta.get('feature_version')}, "
                                 f"server has {F.FEATURE_VERSION}")
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = ORT_NUM_THREADS
            opts.inter_op_num_threads = 1
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(str(self.onnx_path), sess_options=opts,
                                           providers=["CPUExecutionProvider"])
        except Exception as exc:  # keep serving the previous model (or heuristic)
            log.error("Could not load %s: %s", self.onnx_path, exc)
            self._mtime = mtime
            return
        self.session, self.meta, self._mtime = session, meta, mtime
        log.info("Loaded model %s (%s)", self.version, self.onnx_path)


models = ModelHolder(MODEL_DIR)
store = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)

app = FastAPI(title="WikiMasters ML inference", version=__version__,
              docs_url="/docs", redoc_url=None)


@app.exception_handler(redis.RedisError)
async def redis_unavailable(_: Request, exc: redis.RedisError) -> JSONResponse:
    log.warning("Redis error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "redis unavailable"})


class PredictRequest(BaseModel):
    article: str | None = Field(None, max_length=256, description="card name or Wikipedia title")
    series: list[float] | None = Field(None, min_length=1, max_length=5000,
                                       description="daily views, oldest first (bypasses Redis)")
    end_date: date | None = Field(None, description="date of the last value of `series`")


class Prediction(BaseModel):
    article: str | None
    trend_score: int = Field(description="-100 (strong decline) .. +100 (strong rise)")
    trend_raw: float = Field(description="predicted log-ratio: next 7 days vs last 7 days")
    spike_probability: float | None = Field(description="P(+300 % day within 7 days); null for heuristic")
    direction: str
    model: str
    as_of: date
    history_days: int
    window_days: int = F.WINDOW_DAYS
    horizon_days: int = F.HORIZON_DAYS


def resolve_article(name: str) -> str:
    key = F.normalize_title(name)
    if not key:
        raise HTTPException(422, "empty article name")
    return store.hget("wm:alias", key) or key


def load_daily(title: str) -> tuple[np.ndarray, date]:
    raw = store.hgetall(f"wm:views:daily:{title}")
    if not raw:
        raise HTTPException(404, f"no traffic history for '{title}' (not tracked yet?)")
    return F.daily_array(raw)


def score(views: np.ndarray, end_date: date, article: str | None) -> Prediction:
    window = F.last_window(views)
    session = models.get()
    if session is not None:
        x = F.window_features(window, end_date)[None, ...]
        trend, spike = session.run(None, {"series": x})
        trend_raw, spike_prob = float(trend[0]), float(spike[0])
    else:
        trend_raw, spike_prob = F.heuristic_trend(window), None
    ts = F.trend_score(trend_raw)
    return Prediction(
        article=article,
        trend_score=ts,
        trend_raw=round(trend_raw, 4),
        spike_probability=None if spike_prob is None else round(spike_prob, 4),
        direction="up" if ts >= 5 else "down" if ts <= -5 else "flat",
        model=models.version,
        as_of=end_date,
        history_days=int(len(views)),
    )


@app.get("/health")
def health() -> dict:
    models.get()
    try:
        redis_ok = bool(store.ping())
    except redis.RedisError:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok, "model": models.version,
            "model_loaded": models.session is not None, "version": __version__}


@app.get("/predict", response_model=Prediction)
def predict_get(article: str = Query(..., min_length=1, max_length=256)) -> Prediction:
    title = resolve_article(article)
    views, end_date = load_daily(title)
    return score(views, end_date, title)


@app.post("/predict", response_model=Prediction)
def predict_post(req: PredictRequest) -> Prediction:
    if req.series is not None:
        end_date = req.end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
        return score(np.asarray(req.series, dtype=np.float64), end_date, req.article)
    if req.article:
        return predict_get(req.article)
    raise HTTPException(422, "provide `article` or `series`")
