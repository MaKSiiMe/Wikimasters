import gzip
import io
from datetime import date

from wikimedia import WikimediaClient, _map_resolved, parse_dump_lines

DUMP = """\
en Chat 999 0
fr Chat 10 0
fr.m Chat 34 0
fr.m Chat 1 0
fr Cléopâtre_VII 19 0
fr.m Cléopâtre_VII 140 0
fr.wikt Chat 50 0
fr Chaton 7 0
fr Malformed_line
fr Pompéi notanumber 0
"""


def test_parse_dump_lines_filters_project_and_titles():
    gz = gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(DUMP.encode())))
    counts = parse_dump_lines(gz, (b"fr ", b"fr.m "), {"Chat", "Cléopâtre_VII", "Pompéi", "Soleil"})
    assert counts == {"Chat": 45, "Cléopâtre_VII": 159}


def test_dump_prefixes_and_url():
    from datetime import datetime, timezone
    client = WikimediaClient("fr.wikipedia.org", session=None)
    assert client.dump_prefixes == (b"fr ", b"fr.m ")
    url = client.dump_url(datetime(2026, 9, 27, 5, tzinfo=timezone.utc))
    assert url.endswith("/2026/2026-09/pageviews-20260927-050000.gz")


def test_map_resolved_handles_normalization_redirects_and_missing():
    query = {
        "normalized": [{"from": "Saturne_(planète)", "to": "Saturne (planète)"},
                       {"from": "canis lupus", "to": "Canis lupus"}],
        "redirects": [{"from": "Canis lupus", "to": "Loup gris"}],
        "pages": [
            {"ns": 0, "title": "Pas une page", "missing": True},
            {"pageid": 1, "ns": 0, "title": "Saturne (planète)"},
            {"pageid": 2, "ns": 0, "title": "Loup gris"},
        ],
    }
    out = _map_resolved(["Saturne_(planète)", "canis lupus", "Pas une page"], query)
    assert out == {"Saturne_(planète)": "Saturne_(planète)", "canis lupus": "Loup_gris", "Pas une page": None}


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.resp


def test_daily_pageviews_builds_url_and_parses_items():
    session = FakeSession(FakeResp(200, {"items": [
        {"timestamp": "2026092000", "views": 10582}, {"timestamp": "2026092100", "views": 10815}]}))
    client = WikimediaClient("fr.wikipedia.org", session, min_interval=0)
    views = client.daily_pageviews("Saturne_(planète)", date(2026, 9, 20), date(2026, 9, 21))
    assert views == {"20260920": 10582, "20260921": 10815}
    assert session.urls[0].endswith(
        "/per-article/fr.wikipedia.org/all-access/user/Saturne_%28plan%C3%A8te%29/daily/2026092000/2026092100")


def test_daily_pageviews_404_means_no_data():
    client = WikimediaClient("fr.wikipedia.org", FakeSession(FakeResp(404, {})), min_interval=0)
    assert client.daily_pageviews("Chat", date(2026, 9, 20), date(2026, 9, 21)) == {}
