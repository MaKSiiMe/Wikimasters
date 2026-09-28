from datetime import date

from alerts import DiscordNotifier, Evolution, daily_evolution, hourly_evolution


def hours(n, start=2026092800):
    """n consecutive hour labels, newest first (labels don't need to be real dates here)."""
    return [str(start - i) for i in range(n)]


def test_pct_and_spike_threshold():
    evo = Evolution("Chat", "24h", current=4000, previous=1000, as_of="x")
    assert evo.pct == 300.0
    assert evo.is_spike(300, 500)
    assert not evo.is_spike(301, 500)
    assert not evo.is_spike(300, 5000)  # below min views


def test_spike_from_zero_counts_as_new_activity():
    evo = Evolution("Chat", "1d", current=900, previous=0, as_of="x")
    assert evo.pct is None
    assert evo.is_spike(300, 500)
    assert not Evolution("Chat", "1d", 10, 0, "x").is_spike(300, 500)


def test_hourly_evolution_sums_two_windows():
    done = hours(48)
    hourly = {h: 10 for h in done[:24]} | {h: 2 for h in done[24:40]}  # 8 hours at zero
    evo = hourly_evolution("Chat", hourly, done, hourly_since=done[-1])
    assert (evo.current, evo.previous, evo.window, evo.as_of) == (240, 32, "24h", done[0])


def test_hourly_evolution_requires_full_coverage():
    done = hours(48)
    assert hourly_evolution("Chat", {}, done[:47], hourly_since=done[-1]) is None
    assert hourly_evolution("Chat", {}, done, hourly_since=None) is None
    # tracking started after the oldest hour of the window
    assert hourly_evolution("Chat", {}, done, hourly_since=done[10]) is None


def test_daily_evolution():
    daily = {"20260925": 100, "20260926": 150, "20260927": 600}
    evo = daily_evolution("Chat", daily, today=date(2026, 9, 28))
    assert (evo.current, evo.previous, evo.as_of) == (600, 150, "20260927")


def test_daily_evolution_missing_day_is_zero_and_staleness():
    daily = {"20260920": 100, "20260927": 600}
    assert daily_evolution("Chat", daily, today=date(2026, 9, 28)).previous == 0
    assert daily_evolution("Chat", daily, today=date(2026, 10, 5)) is None
    assert daily_evolution("Chat", {"20260927": 5}, today=date(2026, 9, 28)) is None


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload or {}
        self.content = b"x" if payload else b""
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json, timeout):
        self.posts.append((url, json))
        return self.responses.pop(0)


def test_discord_payload_and_retry_on_rate_limit():
    session = FakeSession([FakeResp(429, {"retry_after": 0}), FakeResp(204)])
    notifier = DiscordNotifier("https://discord.test/hook", "fr.wikipedia.org", session=session)
    evo = Evolution("Saturne_(planète)", "24h", 12000, 2000, "2026092811")
    assert notifier.send(evo)
    assert len(session.posts) == 2
    embed = session.posts[0][1]["embeds"][0]
    assert embed["title"].endswith("Saturne (planète)")
    assert embed["url"] == "https://fr.wikipedia.org/wiki/Saturne_%28plan%C3%A8te%29"
    assert "+500 %" in embed["description"]


def test_discord_disabled_without_url():
    notifier = DiscordNotifier("", "fr.wikipedia.org", session=FakeSession([]))
    assert not notifier.enabled
    assert not notifier.send(Evolution("Chat", "1d", 1, 0, "x"))
