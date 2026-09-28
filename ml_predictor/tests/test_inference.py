from datetime import date, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient

import inference_api as api


def seed_article(store, title, days=90, level=100, boost_last=0):
    end = date(2026, 9, 27)
    mapping = {}
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        mapping[day.strftime("%Y%m%d")] = level * (5 if i >= days - boost_last else 1)
    store.hset(f"wm:views:daily:{title}", mapping=mapping)


@pytest.fixture
def client(monkeypatch, tmp_path):
    store = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "models", api.ModelHolder(tmp_path))
    monkeypatch.setattr(api, "RELOAD_CHECK_SECONDS", 0.0)
    return TestClient(api.app), store, tmp_path


def test_health_without_model(client):
    http, _, _ = client
    body = http.get("/health").json()
    assert body["status"] == "ok" and body["redis"] is True
    assert body["model"] == "heuristic" and body["model_loaded"] is False


def test_predict_uses_alias_and_heuristic(client):
    http, store, _ = client
    store.hset("wm:alias", "Canis_lupus", "Loup_gris")
    seed_article(store, "Loup_gris", boost_last=3)
    body = http.get("/predict", params={"article": "canis lupus"}).json()
    assert body["article"] == "Loup_gris"
    assert body["model"] == "heuristic" and body["spike_probability"] is None
    assert body["trend_score"] > 0 and body["direction"] == "up"
    assert body["as_of"] == "2026-09-27" and body["history_days"] == 90


def test_predict_unknown_article_is_404(client):
    http, _, _ = client
    assert http.get("/predict", params={"article": "Inconnu"}).status_code == 404


def test_predict_post_series(client):
    http, _, _ = client
    body = http.post("/predict", json={"series": [100] * 30, "end_date": "2026-09-27"}).json()
    assert body["trend_score"] == 0 and body["direction"] == "flat"
    assert http.post("/predict", json={}).status_code == 422


def test_redis_down_is_503(client, monkeypatch):
    http, _, _ = client
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(api, "store", fakeredis.FakeRedis(server=server, decode_responses=True))
    assert http.get("/predict", params={"article": "Chat"}).status_code == 503


def test_onnx_model_is_loaded_and_hot_reloaded(client):
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")
    import train

    http, store, model_dir = client
    seed_article(store, "Chat")
    torch.manual_seed(0)
    train.export_onnx(train.TrendNet(), model_dir / "model.onnx", opset=17)
    train.write_json_atomic(model_dir / "model.json", {"version": "test-v1", "feature_version": 1})

    body = http.get("/predict", params={"article": "Chat"}).json()
    assert body["model"] == "test-v1"
    assert 0.0 <= body["spike_probability"] <= 1.0
    assert -100 <= body["trend_score"] <= 100

    (model_dir / "model.onnx").unlink()
    assert http.get("/health").json()["model"] == "heuristic"
