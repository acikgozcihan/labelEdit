"""
CaptureEngine entegrasyon testleri — gerçek Chromium + yerel sahte Maps sunucusu.
Eş zamanlılık, sıralama, rate limit, zaman aşımı, bağlantı kopması, hatalı URL,
tekrar önleme, checkpoint/resume ve tarayıcı çökmesi senaryolarını doğrular.
"""
import asyncio
import time
from pathlib import Path

import pytest

from capture_engine import CaptureEngine, CaptureSettings
from data_model import PlaceRecord
from mock_maps_server import MockMapsServer
from screenshot_worker import ScreenshotWorker


def _settings(tmp_path: Path, **kw) -> CaptureSettings:
    base = dict(db_file=tmp_path / "captures.db", screenshots_dir=tmp_path / "shots",
                session_file=None, settle_ms=150, maps_wait_ms=1500, page_timeout_ms=3000,
                max_rate_per_min=1200, backoff_base=0.2, backoff_cap=1.0, cooldown_base=0.5,
                cooldown_cap=2.0, resource_guard=False)
    base.update(kw)
    return CaptureSettings(**base)


def _records(srv: MockMapsServer, names: list[str]) -> list[PlaceRecord]:
    recs = [PlaceRecord.build(i, srv.url(n), n) for i, n in enumerate(names)]
    for p, r in enumerate(recs):
        r.position, r.total = p, len(recs)
    return recs


async def _capture_all(engine: CaptureEngine, recs):
    return await asyncio.gather(*(engine.capture(r) for r in recs))


@pytest.fixture
def engine_factory(tmp_path):
    engines = []

    def make(**kw):
        e = CaptureEngine(_settings(tmp_path, **kw))
        e.start()
        engines.append(e)
        return e
    yield make
    for e in engines:
        e.stop()


def test_parallel_capture_is_faster_than_serial(engine_factory):
    with MockMapsServer(latency=(0.6, 0.6)) as srv:
        eng = engine_factory(start_concurrency=6, max_concurrency=6)
        recs = _records(srv, [f"Mekan {i}" for i in range(18)])
        t = time.perf_counter()
        outs = asyncio.run(_capture_all(eng, recs))
        elapsed = time.perf_counter() - t
    assert all(o.ok for o in outs)
    assert srv.stats["max_inflight"] >= 4                  # gerçekten eş zamanlı
    serial_floor = 18 * (0.6 + 0.15)
    assert elapsed < serial_floor / 2.5, (elapsed, serial_floor)
    assert len({Path(o.file_path).name for o in outs}) == 18   # her mekan ayrı dosya, kayıp yok


def test_worker_serves_in_order_even_when_completion_is_out_of_order(engine_factory):
    with MockMapsServer(latency=(0.05, 0.9)) as srv:
        eng = engine_factory(start_concurrency=5, max_concurrency=5)

        class SM:
            engine = eng

        recs = _records(srv, [f"Sira {i}" for i in range(12)])

        async def go():
            w = ScreenshotWorker(SM(), window=8)
            await w.start(recs)
            items = []
            while not w.is_empty():
                items.append(await w.get_next())
            await w.stop()
            return items
        items = asyncio.run(go())
    assert [i.position for i in items] == list(range(12))
    assert [i.row_index for i in items] == [r.row_index for r in recs]
    assert all(i.error is None and Path(i.file_path).exists() for i in items)


def test_rate_limit_triggers_backoff_and_everything_still_succeeds(engine_factory):
    # Sunucu: 1 sn'de en fazla 3 istek, aşılırsa 1.5 sn ceza (Google "sorry" sayfası)
    with MockMapsServer(latency=(0.1, 0.2), rate_limit=3, window=1.0, penalty=1.5) as srv:
        eng = engine_factory(start_concurrency=8, max_concurrency=8, max_attempts=6)
        recs = _records(srv, [f"Yogun {i}" for i in range(20)])
        outs = asyncio.run(_capture_all(eng, recs))
        stats = eng.stats()
    assert srv.stats["rate_limited"] > 0                       # limit gerçekten tetiklendi
    ctrl = stats["controller"]
    assert ctrl["decreases"] >= 1 and ctrl["cooldowns"] >= 1   # eş zamanlılık otomatik düştü
    assert sum(o.ok for o in outs) == 20, [o.error for o in outs if not o.ok]


def test_timeout_drop_flaky_and_invalid_urls(engine_factory):
    with MockMapsServer(latency=(0.05, 0.1)) as srv:
        eng = engine_factory(start_concurrency=4, max_concurrency=4, max_attempts=2, page_timeout_ms=1500)
        names = ["timeout A", "drop B", "flaky C", "notfound D", "Normal E", "Normal F"]
        recs = _records(srv, names)
        recs.append(PlaceRecord.build(99, "https://example.com/not-maps", "bozuk"))
        before = srv.stats["requests"]
        t = time.perf_counter()
        outs = dict(zip([r.name for r in recs], asyncio.run(_capture_all(eng, recs))))
        elapsed = time.perf_counter() - t
        requests_made = srv.stats["requests"] - before
        failures = {f["url"]: f for f in eng.store.failures()}

    assert outs["Normal E"].ok and outs["Normal F"].ok
    assert outs["flaky C"].ok and outs["flaky C"].attempts == 2          # 503 → tekrar → başarı
    assert outs["timeout A"].error_kind == "transient" and outs["timeout A"].attempts == 2
    assert outs["drop B"].error_kind == "transient"
    assert outs["notfound D"].error_kind == "permanent" and outs["notfound D"].attempts == 1
    assert outs["bozuk"].error_kind == "invalid_url" and outs["bozuk"].attempts == 0
    assert elapsed < 12                                   # zaman aşımı diğerlerini bloke etmedi
    assert requests_made <= 2 + 4 + 2 + 1 + 1 + 1          # geçersiz URL için ağa çıkılmadı
    # Başarısızlar kayıt altında (tür, deneme, sonraki deneme zamanı)
    assert failures[recs[0].url]["error_kind"] == "transient" and failures[recs[0].url]["next_retry_at"]
    assert failures[recs[3].url]["next_retry_at"] is None


def test_duplicate_places_fetched_once(engine_factory):
    with MockMapsServer(latency=(0.3, 0.3)) as srv:
        eng = engine_factory(start_concurrency=4, max_concurrency=4)
        url = srv.url("Ayni Yer")
        recs = [PlaceRecord.build(i, url, "Ayni Yer") for i in range(5)]
        before = srv.stats["requests"]
        outs = asyncio.run(_capture_all(eng, recs))
        assert srv.stats["requests"] - before == 1
    assert all(o.ok for o in outs) and len({o.file_path for o in outs}) == 1


def test_checkpoint_resume_skips_completed_work(tmp_path):
    with MockMapsServer(latency=(0.05, 0.1)) as srv:
        recs = _records(srv, [f"Devam {i}" for i in range(8)])
        e1 = CaptureEngine(_settings(tmp_path, start_concurrency=4, max_concurrency=4))
        e1.start()
        try:
            first = asyncio.run(_capture_all(e1, recs[:5]))      # "kesinti" öncesi 5 tamam
        finally:
            e1.stop()
        before = srv.stats["requests"]
        e2 = CaptureEngine(_settings(tmp_path, start_concurrency=4, max_concurrency=4))   # yeni süreç
        e2.start()
        try:
            second = asyncio.run(_capture_all(e2, recs))
        finally:
            e2.stop()
        new_requests = srv.stats["requests"] - before
    assert all(o.ok for o in first + second)
    assert [o.from_cache for o in second] == [True] * 5 + [False] * 3
    assert new_requests == 3                                    # yalnızca eksik olanlar


def test_recovers_after_browser_crash(engine_factory):
    with MockMapsServer(latency=(0.05, 0.1)) as srv:
        eng = engine_factory()
        recs = _records(srv, ["Once", "Sonra"])
        assert eng.capture_sync(recs[0]).ok
        eng.submit(eng._browser.close()).result(timeout=10)     # tarayıcı çöktü
        out = eng.capture_sync(recs[1], timeout=60)
    assert out.ok
    assert eng.stats().get("browser_restarts", 0) == 1
