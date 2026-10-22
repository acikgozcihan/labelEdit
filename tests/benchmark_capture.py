"""
Eski ve yeni ekran görüntüsü hattının karşılaştırmalı ölçümü (yerel sahte Maps sunucusu).

    python tests/benchmark_capture.py --scenario all --n 60

Eski sürüm git'ten (varsayılan: ilk sürüm 7e4ead2) geçici klasöre çıkarılır.
Her varyant ayrı bir alt süreçte çalışır (modül çakışması olmasın); sahte sunucu
ana süreçte paylaşılır. Kullanıcı "anında" ilerliyormuş gibi tüketilir; böylece
ölçülen değer hattın kendi verimidir.

Senaryolar:
  clean      — tüm bağlantılar sağlam
  faults     — %5 zaman aşımı, %5 bağlantı kopması, %5 geçici 503, %5 hatalı URL
  ratelimit  — sunucu saniyede 2 isteği aşınca 5 sn ceza verir (Google "sorry" benzeri)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

PAGE_TIMEOUT_MS = 10_000


def scenario_names(scenario: str, n: int) -> list[str]:
    names = []
    for i in range(n):
        if scenario == "faults":
            k = i % 20
            prefix = {3: "timeout", 7: "drop", 11: "flaky", 15: "invalid"}.get(k, "Mekan")
        else:
            prefix = "Mekan"
        names.append(f"{prefix} {i}")
    return names


# ── Alt süreç: tek bir varyantı çalıştır ─────────────────────────────────────
class ResourceSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        import psutil
        self.psutil = psutil
        self.proc = psutil.Process()
        self.stop_evt = threading.Event()
        self.peak_rss = 0
        self.cpu: dict[int, float] = {}

    def sample(self) -> None:
        try:
            procs = [self.proc] + self.proc.children(recursive=True)
        except self.psutil.Error:
            return
        rss = 0
        for p in procs:
            try:
                rss += p.memory_info().rss
                t = p.cpu_times()
                self.cpu[p.pid] = max(self.cpu.get(p.pid, 0.0), t.user + t.system)
            except self.psutil.Error:
                pass
        self.peak_rss = max(self.peak_rss, rss)

    def run(self) -> None:
        while not self.stop_evt.is_set():
            self.sample()
            self.stop_evt.wait(0.25)

    def stop(self) -> dict:
        self.stop_evt.set()
        self.sample()
        return {"peak_rss_mb": round(self.peak_rss / 1e6), "cpu_sec": round(sum(self.cpu.values()), 1)}


def run_variant(args: argparse.Namespace) -> None:
    work = Path(args.workdir)
    names = scenario_names(args.scenario, args.n)
    base = f"http://127.0.0.1:{args.port}"

    def url(name: str) -> str:
        import zlib
        if name.startswith("invalid"):
            return f"https://example.invalid/place/{name.replace(' ', '+')}"
        return f"{base}/maps/place/{name.replace(' ', '+')}/data=!4m2!3m1!1s0x0:0x{zlib.crc32(name.encode()):x}"

    records = [{"row_index": i, "url": url(nm), "name": nm, "position": i, "total": len(names)}
               for i, nm in enumerate(names)]

    if args.variant == "legacy":
        sys.path.insert(0, str(Path(args.legacy_src)))
        import config as legacy_config  # eski sürümün config'i
        legacy_config.SCREENSHOT_TIMEOUT = PAGE_TIMEOUT_MS
        from screenshot_worker import ScreenshotWorker  # noqa: E402
        from session_manager import SessionManager      # noqa: E402
        sm = SessionManager()
        shots_dir = legacy_config.SCREENSHOTS_DIR
    else:
        os.environ.update({
            "LABELEDIT_DATA_DIR": str(work / "data"), "LABELEDIT_SCREENSHOTS_DIR": str(work / "shots"),
            "LABELEDIT_SESSIONS_DIR": str(work / "sessions"), "LABELEDIT_EXTRA_MAPS_HOSTS": "127.0.0.1",
            "LABELEDIT_PAGE_TIMEOUT_MS": str(PAGE_TIMEOUT_MS),
        })
        presets = {
            "new-default": {},
            "new-fast": {"LABELEDIT_MAX_CONCURRENCY": "8", "LABELEDIT_START_CONCURRENCY": "4",
                         "LABELEDIT_MAX_RATE_PER_MIN": "240"},
            "fixed-8": {"LABELEDIT_MIN_CONCURRENCY": "8", "LABELEDIT_START_CONCURRENCY": "8",
                        "LABELEDIT_MAX_CONCURRENCY": "8", "LABELEDIT_MAX_RATE_PER_MIN": "100000",
                        "LABELEDIT_COOLDOWN_BASE": "0", "LABELEDIT_COOLDOWN_CAP": "0"},
        }
        os.environ.update(presets[args.variant])
        sys.path.insert(0, str(ROOT / "src"))
        import config  # noqa: E402
        from screenshot_worker import ScreenshotWorker  # noqa: E402
        from session_manager import SessionManager      # noqa: E402
        sm = SessionManager()
        shots_dir = config.SCREENSHOTS_DIR

    import logging
    logging.disable(logging.CRITICAL)
    sampler = ResourceSampler()
    sampler.start()
    sm.start()

    async def consume() -> tuple[list, float]:
        w = ScreenshotWorker(sm)
        t0 = time.perf_counter()
        await w.start(records)
        items = []
        while not w.is_empty():
            it = await w.get_next()
            if it is None:
                items.append(None)
                continue
            items.append({"pos": it.position, "name": it.name, "ok": it.error is None and bool(it.file_path)
                          and Path(it.file_path).exists(), "error": it.error, "file": it.file_path})
        elapsed = time.perf_counter() - t0
        await w.stop() if args.variant != "legacy" else None
        return items, elapsed

    items, elapsed = asyncio.run(consume())
    sm.stop()
    res = sampler.stop()
    files = [Path(i["file"]) for i in items if i and i["ok"]]
    sizes = [f.stat().st_size for f in files if f.exists()]
    print("RESULT " + json.dumps({
        "items": items, "elapsed": elapsed, **res,
        "avg_kb": round(sum(sizes) / len(sizes) / 1024, 1) if sizes else 0,
    }))


# ── Ana süreç ────────────────────────────────────────────────────────────────
def extract_legacy(ref: str, dest: Path) -> Path:
    src = dest / "src"
    src.mkdir(parents=True, exist_ok=True)
    for f in ("config.py", "session_manager.py", "screenshot_worker.py"):
        code = subprocess.run(["git", "show", f"{ref}:src/{f}"], cwd=ROOT, capture_output=True, check=True).stdout
        (src / f).write_bytes(code)
    return src


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="all", choices=["clean", "faults", "ratelimit", "all"])
    p.add_argument("--variants", default="legacy,new-default,new-fast,fixed-8")
    p.add_argument("--n", type=int, default=60)
    p.add_argument("--legacy-ref", default="7e4ead2")
    p.add_argument("--legacy-src")
    p.add_argument("--out")
    # alt süreç parametreleri
    p.add_argument("--run-variant", dest="variant")
    p.add_argument("--port", type=int)
    p.add_argument("--workdir")
    args = p.parse_args()
    if args.variant:
        return run_variant(args)

    from mock_maps_server import MockMapsServer
    tmp = Path(tempfile.mkdtemp(prefix="labeledit_bench_"))
    legacy_src = Path(args.legacy_src) if args.legacy_src else extract_legacy(args.legacy_ref, tmp / "legacy")
    scenarios = ["clean", "faults", "ratelimit"] if args.scenario == "all" else [args.scenario]
    rows = []
    for sc in scenarios:
        for variant in args.variants.split(","):
            server_kw = {"latency": (0.2, 0.6)}
            if sc == "ratelimit":
                server_kw.update(rate_limit=2, window=1.0, penalty=5.0)
            with MockMapsServer(**server_kw) as srv:
                wd = tmp / f"{sc}_{variant}"
                wd.mkdir(parents=True, exist_ok=True)
                cmd = [sys.executable, __file__, "--run-variant", variant, "--scenario", sc, "--n", str(args.n),
                       "--port", str(srv.port), "--workdir", str(wd), "--legacy-src", str(legacy_src)]
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONWARNINGS": "ignore"}
                t = time.perf_counter()
                proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(wd))
                line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
                if not line:
                    print(f"{sc}/{variant} başarısız:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
                    continue
                r = json.loads(line[7:])
                items = [i for i in r["items"] if i]
                ok = sum(i["ok"] for i in items)
                # Yanlış başarı: 503 sayfasının görüntüsünü "başarılı" saymak (sunucu o mekana tek kez cevap verdi)
                false_ok = sum(1 for i in items if i["ok"] and i["name"].startswith("flaky")
                               and srv.flaky_seen.get(i["name"].lower(), 0) <= MockMapsServer.FLAKY_FAILS)
                ordered = [i["pos"] for i in items] == list(range(len(items)))
                row = {
                    "scenario": sc, "variant": variant, "n": args.n, "wall_sec": round(r["elapsed"], 1),
                    "success": ok - false_ok, "false_success": false_ok, "errors": len(items) - ok,
                    "success_per_min": round((ok - false_ok) / r["elapsed"] * 60, 1),
                    "error_rate": round((len(items) - ok + false_ok) / len(items), 3),
                    "in_order": ordered, "lost": args.n - len(items),
                    "server_requests": srv.stats["requests"], "server_rate_limited": srv.stats["rate_limited"],
                    "max_parallel": srv.stats["max_inflight"],
                    "peak_rss_mb": r["peak_rss_mb"], "cpu_sec": r["cpu_sec"],
                    "cpu_sec_per_success": round(r["cpu_sec"] / max(1, ok - false_ok), 2),
                    "avg_image_kb": r["avg_kb"], "process_sec": round(time.perf_counter() - t, 1),
                }
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
