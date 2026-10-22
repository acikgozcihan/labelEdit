"""Maps URL ayrıştırma ve adaptif rate kontrol birim testleri."""
import asyncio
import random
import time

from data_model import PlaceRecord, parse_maps_url
from rate_control import AdaptiveController, backoff_delay


# ── URL ayrıştırma ───────────────────────────────────────────────────────────
def test_feature_id_key_ignores_zoom_and_viewport():
    a = parse_maps_url("https://www.google.com/maps/place/Cafe/@41.0,29.0,17z/data=!4m6!3m5!1s0x14cab:0x9a!8m2")
    b = parse_maps_url("https://www.google.com.tr/maps/place/Cafe/@41.1,29.1,12z/data=!3m1!1s0x14cab:0x9a")
    assert a.error is None and a.place_key == b.place_key == "fid:0x14cab:0x9a"
    assert a.name_hint == "Cafe"


def test_query_params_and_short_links():
    assert parse_maps_url("https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJabc").place_key == "pid:ChIJabc"
    assert parse_maps_url("https://maps.google.com/?cid=123456").place_key == "cid:123456"
    assert parse_maps_url("https://maps.app.goo.gl/AbCdEf").error is None
    assert parse_maps_url("www.google.com/maps/place/X").url.startswith("https://www.google.com/maps/place/X")


def test_invalid_urls_rejected_without_network():
    for bad in ("", None, "bilinmiyor", "https://example.com/maps/place/x", "ftp://google.com/maps",
                "https://goo.gl/abc", "https://www.google.com/search?q=cafe"):
        assert parse_maps_url(bad).error, bad
    rec = PlaceRecord.build(3, "https://evil.com/x", "Ad")
    assert rec.url_error and rec.place_key.startswith("invalid:")


def test_hyperlink_formula_and_place_id_column_override():
    info = parse_maps_url('=HYPERLINK("https://www.google.com/maps/place/A/data=!1s0x1:0x2","Aç")')
    assert info.place_key == "fid:0x1:0x2"
    rec = PlaceRecord.build(1, "https://www.google.com/maps/place/A", None, None, "ChIJ1234567890abc")
    assert rec.place_key == "pid:ChIJ1234567890abc" and rec.name == "A"


# ── Backoff + jitter ─────────────────────────────────────────────────────────
def test_backoff_full_jitter_bounds():
    rng = random.Random(1)
    for attempt in range(8):
        cap = min(30.0, 1.0 * 2 ** attempt)
        vals = [backoff_delay(attempt, 1.0, 30.0, rng) for _ in range(200)]
        assert all(0 <= v <= cap for v in vals)
        assert max(vals) > cap * 0.7       # dağılım gerçekten yayılıyor (jitter)


# ── AIMD kontrolör ───────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


def test_additive_increase_up_to_max():
    async def go():
        c = AdaptiveController(1, 1, 5, max_rate_per_min=0)
        for _ in range(40):
            t = await c.acquire()
            await c.release()
            await c.on_success()
        return c
    c = _run(go())
    assert int(c.limit) == 5 and c.stats["increases"] == 4


def test_rate_limit_halves_once_per_epoch_and_sets_cooldown():
    async def go():
        c = AdaptiveController(1, 8, 8, max_rate_per_min=0, cooldown_base=10, cooldown_cap=60)
        tickets = [await c.acquire() for _ in range(8)]
        for t in tickets:
            await c.release()
        for t in tickets:                     # 8 eş zamanlı istek aynı anda engellendi
            c.on_rate_limited(t)
        return c
    c = _run(go())
    assert int(c.limit) == 4                  # 8 kez değil, bir kez yarılandı
    assert c.stats["decreases"] == 1
    assert 5 <= c.cooldown_until - time.monotonic() <= 10


def test_isolated_transient_errors_do_not_collapse_pool():
    async def go():
        c = AdaptiveController(1, 6, 6, max_rate_per_min=0)
        for i in range(30):
            t = await c.acquire()
            await c.release()
            if i % 10 == 0:
                c.on_transient(t)             # %10 bozuk bağlantı
            else:
                await c.on_success()
        return c
    assert int(_run(go()).limit) == 6


def test_transient_burst_reduces_concurrency():
    async def go():
        c = AdaptiveController(1, 6, 6, max_rate_per_min=0)
        for _ in range(10):
            t = await c.acquire()
            await c.release()
            c.on_transient(t)
        return c
    assert int(_run(go()).limit) < 6


def test_cooldown_blocks_new_requests():
    async def go():
        c = AdaptiveController(1, 2, 2, max_rate_per_min=0, cooldown_base=0.6, cooldown_cap=0.6,
                               rng=random.Random(0))
        t = await c.acquire()
        await c.release()
        cd = c.on_rate_limited(t)
        t0 = time.monotonic()
        await c.acquire()
        return cd, time.monotonic() - t0
    cd, waited = _run(go())
    assert waited >= cd - 0.05


def test_concurrency_limit_is_enforced():
    async def go():
        c = AdaptiveController(1, 3, 3, max_rate_per_min=0)
        peak = 0

        async def job():
            nonlocal peak
            await c.acquire()
            peak = max(peak, c.active)
            await asyncio.sleep(0.02)
            await c.release()
        await asyncio.gather(*(job() for _ in range(20)))
        return peak
    assert _run(go()) == 3


def test_resource_probe_blocks_increase():
    class Busy:
        def state(self):
            return (False, False)

    async def go():
        c = AdaptiveController(1, 2, 8, max_rate_per_min=0, probe=Busy())
        for _ in range(30):
            await c.acquire()
            await c.release()
            await c.on_success()
        return c
    assert int(_run(go()).limit) == 2
