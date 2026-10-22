"""
Proje V1 — Adaptif Eş Zamanlılık ve Rate Limit Kontrolü

Amaç "en fazla eş zamanlı istek" değil, birim zamanda BAŞARIYLA işlenen
mekan sayısını en yükseğe çıkarmaktır. Bunun için TCP'deki tıkanıklık
kontrolüne benzer bir AIMD (Additive Increase / Multiplicative Decrease)
şeması kullanılır:

  • Başarı   → her `limit` kadar başarıda limit +1 (kaynaklar uygunsa),
               sayfa açılış aralığı %15 kısalır (tavan hıza kadar).
  • Rate limit (captcha / 429) → limit yarıya iner, aralık 2 katına çıkar,
               tüm havuz üstel + jitter'lı bir soğuma süresi bekler.
  • Geçici hata (timeout, bağlantı kopması) → yalnızca SON sonuçlardaki hata
               oranı yüksekse (sistemik sorun) limit 1 azalır. Tek tük bozuk
               bağlantılar tüm havuzu yavaşlatmaz.

Aynı anda başlamış N isteğin hepsi aynı tıkanıklığa takıldığında limitin N
kez yarılanmaması için "epoch" kullanılır: yalnızca son değişiklikten SONRA
başlamış bir isteğin hatası limiti tekrar düşürebilir.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:   # isteğe bağlı: kurulu değilse kaynak kontrolü devre dışı kalır
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random = random) -> float:
    """Üstel backoff + 'full jitter' (AWS mimari önerisi): U(0, min(cap, base·2^attempt))."""
    return rng.uniform(0, min(cap, base * (2 ** max(0, attempt))))


class ResourceProbe:
    """CPU/RAM durumunu ölçer; psutil yoksa her zaman 'uygun' döner."""

    def __init__(self, cpu_high: float, min_free_mb: float) -> None:
        self.cpu_high = cpu_high
        self.min_free_mb = min_free_mb
        self._last = 0.0
        self._last_state = (True, False)
        if psutil:
            psutil.cpu_percent(interval=None)   # ilk ölçümü başlat

    def state(self) -> tuple[bool, bool]:
        """(artırmaya uygun mu, aşırı yük var mı) — en fazla saniyede bir ölçülür."""
        if not psutil:
            return (True, False)
        now = time.monotonic()
        if now - self._last < 1.0:
            return self._last_state
        self._last = now
        cpu = psutil.cpu_percent(interval=None)
        free_mb = psutil.virtual_memory().available / 1e6
        ok = cpu < self.cpu_high and free_mb > self.min_free_mb
        overloaded = cpu > min(99.0, self.cpu_high + 7) or free_mb < self.min_free_mb / 2
        self._last_state = (ok, overloaded)
        return self._last_state


@dataclass
class Ticket:
    epoch: int
    started: float


class AdaptiveController:
    def __init__(
        self,
        min_concurrency: int = 1,
        start_concurrency: int = 2,
        max_concurrency: int = 4,
        max_rate_per_min: float = 60.0,
        cooldown_base: float = 15.0,
        cooldown_cap: float = 300.0,
        probe: Optional[ResourceProbe] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        error_window: int = 12,
        error_ratio: float = 0.3,
    ) -> None:
        self.min_c = max(1, min_concurrency)
        self.max_c = max(self.min_c, max_concurrency)
        self.limit = float(min(self.max_c, max(self.min_c, start_concurrency)))
        self.floor_interval = 60.0 / max_rate_per_min if max_rate_per_min > 0 else 0.0
        self.interval = self.floor_interval
        self.max_interval = max(self.floor_interval * 16, 8.0)
        self.cooldown_base = cooldown_base
        self.cooldown_cap = cooldown_cap
        self.probe = probe
        self.clock = clock
        self.rng = rng or random.Random()

        self.active = 0
        self.epoch = 0
        self._recent: deque[bool] = deque(maxlen=error_window)   # True = geçici hata
        self._error_ratio = error_ratio
        self._successes_since_change = 0
        self._consecutive_blocks = 0
        self.cooldown_until = 0.0
        self._next_start = 0.0
        self._cond: Optional[asyncio.Condition] = None
        self._pace_lock: Optional[asyncio.Lock] = None
        self.stats = {"success": 0, "rate_limited": 0, "transient": 0, "permanent": 0,
                      "increases": 0, "decreases": 0, "cooldowns": 0, "peak_limit": int(self.limit)}

    # asyncio primitifleri kontrolörün kullanıldığı loop içinde oluşturulur
    def _prims(self) -> tuple[asyncio.Condition, asyncio.Lock]:
        if self._cond is None:
            self._cond = asyncio.Condition()
            self._pace_lock = asyncio.Lock()
        return self._cond, self._pace_lock  # type: ignore[return-value]

    async def acquire(self) -> Ticket:
        cond, pace = self._prims()
        async with cond:
            await cond.wait_for(lambda: self.active < int(self.limit))
            self.active += 1
        try:
            # Sayfa açılışlarını aralıklandır + soğuma süresine uy
            async with pace:
                while True:
                    now = self.clock()
                    wait = max(self.cooldown_until, self._next_start) - now
                    if wait <= 0:
                        break
                    await asyncio.sleep(min(wait, 1.0))
                self._next_start = self.clock() + self.interval * self.rng.uniform(0.8, 1.2)
        except BaseException:
            await self.release()
            raise
        return Ticket(self.epoch, self.clock())

    async def release(self) -> None:
        cond, _ = self._prims()
        async with cond:
            self.active = max(0, self.active - 1)
            cond.notify_all()

    async def _notify(self) -> None:
        cond, _ = self._prims()
        async with cond:
            cond.notify_all()

    # ── Geri bildirim ────────────────────────────────────────────────────────
    async def on_success(self) -> None:
        self.stats["success"] += 1
        self._recent.append(False)
        self._consecutive_blocks = 0
        self._successes_since_change += 1
        ok, overloaded = self.probe.state() if self.probe else (True, False)
        if overloaded and self.limit > self.min_c:
            self._decrease(self.limit - 1)
            return
        if self._successes_since_change >= max(1, math.ceil(self.limit)):
            self._successes_since_change = 0
            if ok and self.limit < self.max_c:
                self.limit += 1
                self.stats["increases"] += 1
                self.stats["peak_limit"] = max(self.stats["peak_limit"], int(self.limit))
                await self._notify()
            self.interval = max(self.floor_interval, self.interval * 0.85)

    def on_rate_limited(self, ticket: Optional[Ticket]) -> float:
        """Rate limit: limit yarıya, aralık 2 katına, havuz soğumaya. Dönen: soğuma süresi (sn)."""
        self.stats["rate_limited"] += 1
        if ticket is not None and ticket.epoch != self.epoch:
            # Bu istek önceki düşüşten önce başlamıştı; zaten tepki verildi
            return max(0.0, self.cooldown_until - self.clock())
        self._consecutive_blocks += 1
        self._decrease(math.floor(self.limit / 2))
        self.interval = min(self.max_interval, max(self.interval, 0.5) * 2)
        raw = min(self.cooldown_cap, self.cooldown_base * (2 ** (self._consecutive_blocks - 1)))
        cooldown = raw * self.rng.uniform(0.5, 1.0)          # "equal jitter"
        self.cooldown_until = max(self.cooldown_until, self.clock() + cooldown)
        self.stats["cooldowns"] += 1
        return cooldown

    def on_transient(self, ticket: Optional[Ticket]) -> None:
        self.stats["transient"] += 1
        self._recent.append(True)
        if ticket is not None and ticket.epoch != self.epoch:
            return
        n = len(self._recent)
        if n >= min(6, self._recent.maxlen) and sum(self._recent) / n >= self._error_ratio:
            self._decrease(self.limit - 1)

    def recent_error_ratio(self) -> float:
        return sum(self._recent) / len(self._recent) if self._recent else 0.0

    def on_permanent(self) -> None:
        self.stats["permanent"] += 1

    def _decrease(self, new_limit: float) -> None:
        new_limit = max(self.min_c, new_limit)
        if new_limit < self.limit:
            self.stats["decreases"] += 1
        self.limit = float(new_limit)
        self.epoch += 1
        self._successes_since_change = 0

    def snapshot(self) -> dict:
        return {
            "concurrency_limit": int(self.limit),
            "active": self.active,
            "min_interval_sec": round(self.interval, 3),
            "cooldown_remaining_sec": round(max(0.0, self.cooldown_until - self.clock()), 1),
            "recent_error_ratio": round(self.recent_error_ratio(), 2),
            **self.stats,
        }
