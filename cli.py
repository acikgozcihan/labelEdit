"""
Proje V1 — Komut satırı araçları

  python cli.py analyze [dosya ...] [--json]
      input/ klasöründeki Excel/CSV dosyalarının sayfa, başlık satırı ve
      otomatik tespit edilen kolon rollerini (Maps bağlantısı, kontrol/durum,
      isim, place_id) güven skorlarıyla raporlar.

  python cli.py prefetch [--flow edit|label] [--file AD] [--start-index N] [--limit N]
                         [--max-concurrency N] [--max-rate N] [--retry-failed]
      Bekleyen tüm mekanların ekran görüntülerini toplu ve eş zamanlı hazırlar.
      Her tamamlanan mekan anında kaydedilir (checkpoint); Ctrl+C ile kesip aynı
      komutla kaldığı yerden devam edebilirsiniz. Önceden hazırlanmış mekanlar
      tekrar yakalanmaz. Arayüzde bu mekanlar anında açılır.

  python cli.py failures [--limit N]
      Başarısız yakalamaları (hata türü, deneme sayısı, son hata) listeler.

  python cli.py prune [--orphans] [--older-than-hours H] [--apply]
      Süresi dolmuş önbellek dosyalarını (ve --orphans ile eski sürümden kalan,
      hiçbir kayda bağlı olmayan ekran görüntülerini) listeler; --apply ile siler.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import config  # noqa: E402
from data_model import ColumnDetectionError, ROLE_LABELS_TR, ColumnRole  # noqa: E402


def cmd_analyze(args: argparse.Namespace) -> int:
    from excel_schema import analyze_file
    files = [config.resolve_input_file(f) for f in args.files] if args.files else config.list_input_files()
    if not files:
        print(f"input/ klasöründe dosya yok: {config.INPUT_DIR}")
        return 1
    results = []
    for f in files:
        t = time.perf_counter()
        prof = analyze_file(f, use_cache=not args.no_cache)
        results.append(prof.to_dict())
        if args.json:
            continue
        print(f"\n== {f.name}   ({time.perf_counter() - t:.2f} sn)")
        for sh in prof.sheets:
            det = ", ".join(f"{k}={v}" for k, v in sh["detected"].items()) or "-"
            print(f"   sayfa '{sh['sheet']}': ~{sh['rows']} satır, başlık satırı {sh['header_row']} | {det}")
        if prof.error:
            print(f"   HATA: {prof.error}")
        s = prof.schema
        if s and not prof.error:
            print(f"   -> Veri sayfası: '{s.sheet}' (başlık satırı {s.header_row})")
            for role in ColumnRole:
                m = s.matches.get(role.value)
                label = ROLE_LABELS_TR[role]
                if m:
                    print(f"     {label:24s}: '{m.column}'  güven={m.confidence:.2f}  [{m.method}] {m.evidence}")
                else:
                    print(f"     {label:24s}: (bulunamadı)")
            for w in s.warnings:
                print(f"     UYARI: {w}")
            for e in s.errors:
                print(f"     HATA: {e}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def _load_records(args: argparse.Namespace):
    if args.flow == "edit":
        from edit_data_manager import EditDataManager
        m = EditDataManager(args.file)
        recs = m.get_place_records(args.start_index)
        name = m.source_path.name
        m.close()
    else:
        from data_manager import DataManager
        m = DataManager(args.file, start_flusher=False, read_only=True)
        recs = m.get_place_records(args.start_index)
        name = m.schema.file
        m.close()
    return name, recs


def cmd_prefetch(args: argparse.Namespace) -> int:
    from capture_engine import CaptureEngine, CaptureSettings

    try:
        source, records = _load_records(args)
    except (FileNotFoundError, ColumnDetectionError) as e:
        print(f"HATA: {e}")
        return 2

    settings = CaptureSettings()
    if args.max_concurrency:
        settings.max_concurrency = args.max_concurrency
        settings.start_concurrency = min(settings.start_concurrency, args.max_concurrency)
    if args.max_rate:
        settings.max_rate_per_min = args.max_rate
    engine = CaptureEngine(settings)
    store = engine.store

    # Tekrarları ayıkla, tamamlananları ve tekrar denenmemesi gerekenleri atla
    seen: set[str] = set()
    todo, cached, dup, invalid, blocked = [], 0, 0, 0, 0
    for r in records:
        if r.place_key in seen:
            dup += 1
            continue
        seen.add(r.place_key)
        if r.url_error:
            invalid += 1
            store.mark_failed(r.place_key, r.url, "invalid_url", r.url_error, 0, None)
            continue
        if store.get_ok_file(r.place_key):
            cached += 1
            continue
        until = store.retry_blocked_until(r.place_key)
        if until is not None and (until == float("inf") or not args.retry_failed):
            blocked += 1
            continue
        todo.append(r)
    if args.limit:
        todo = todo[:args.limit]

    print(f"Kaynak: {source} | akış: {args.flow}")
    print(f"Kayıt: {len(records)} | tekrar (aynı mekan): {dup} | zaten hazır: {cached} | "
          f"geçersiz URL: {invalid} | önceki hata (atlandı): {blocked} | işlenecek: {len(todo)}")
    if not todo:
        print("Yapılacak iş yok.")
        return 0

    engine.start()
    done = {"ok": 0, "failed": 0}
    t0 = time.monotonic()

    async def run() -> None:
        queue: asyncio.Queue = asyncio.Queue()
        for r in todo:
            queue.put_nowait(r)

        async def worker() -> None:
            while True:
                try:
                    r = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                out = await engine.capture(r)
                done["ok" if out.ok else "failed"] += 1

        async def reporter() -> None:
            while True:
                await asyncio.sleep(args.report_every)
                n = done["ok"] + done["failed"]
                el = time.monotonic() - t0
                rate = done["ok"] / el * 60 if el else 0
                eta = (len(todo) - n) / (n / el) if n else float("nan")
                c = engine.controller.snapshot()
                print(f"  [{el:6.0f} sn] {n}/{len(todo)} | başarılı {done['ok']} | hata {done['failed']} | "
                      f"{rate:.1f} mekan/dk | eş zamanlılık {c['concurrency_limit']} "
                      f"(aktif {c['active']}) | soğuma {c['cooldown_remaining_sec']} sn | ETA {eta / 60:.1f} dk",
                      flush=True)

        # Sınırlı sayıda işçi: binlerce kayıt için binlerce bekleyen görev açılmaz
        workers = [asyncio.create_task(worker()) for _ in range(max(2, settings.max_concurrency * 2))]
        rep = asyncio.create_task(reporter())
        try:
            await asyncio.gather(*workers)
        finally:
            rep.cancel()

    interrupted = False
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        interrupted = True
        print("\nKesildi. Tamamlanan yakalamalar kaydedildi; aynı komutla devam edebilirsiniz.")
    finally:
        engine.stop()
    el = time.monotonic() - t0
    n = done["ok"] + done["failed"]
    print("\n-- Özet ------------------------------------------")
    print(f"Süre: {el:.1f} sn | işlenen: {n} | başarılı: {done['ok']} | hata: {done['failed']} "
          f"| hata oranı: {done['failed'] / n:.1%}" if n else "İşlenen yok.")
    if el and n:
        print(f"Verim: {done['ok'] / el * 60:.1f} başarılı mekan/dk")
    st = engine.stats()
    print(f"Motor: {json.dumps({k: v for k, v in st.items() if k != 'controller'}, ensure_ascii=False)}")
    print(f"Kontrol: {json.dumps(st['controller'], ensure_ascii=False)}")
    return 130 if interrupted else 0


def cmd_failures(args: argparse.Namespace) -> int:
    from capture_store import CaptureStore
    store = CaptureStore(config.CAPTURE_DB_FILE, config.SCREENSHOTS_DIR, config.CAPTURE_CACHE_TTL_HOURS)
    print(json.dumps(store.stats(), ensure_ascii=False))
    for f in store.failures(args.limit):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["updated_at"]))
        print(f"{when} | {f['error_kind']:12s} | deneme {f['attempts']} (toplam {f['total_attempts']}) | "
              f"{f['url'][:70]} | {f['error'][:80] if f['error'] else ''}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from capture_store import CaptureStore
    store = CaptureStore(config.CAPTURE_DB_FILE, config.SCREENSHOTS_DIR, config.CAPTURE_CACHE_TTL_HOURS)
    expired = store.prune_expired(dry_run=not args.apply)
    targets = list(expired)
    if args.orphans:
        known = store.known_files()
        cutoff = time.time() - args.older_than_hours * 3600
        for f in config.SCREENSHOTS_DIR.iterdir():
            if f.is_file() and f.name not in known and f.stat().st_mtime < cutoff:
                targets.append(f)
    size = sum(f.stat().st_size for f in targets if f.exists())
    print(f"{'Silindi' if args.apply else 'Silinecek (deneme modu)'}: {len(targets)} dosya, {size / 1e6:.1f} MB")
    if args.apply:
        for f in targets:
            f.unlink(missing_ok=True)
    else:
        print("Gerçekten silmek için --apply ekleyin.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="Google Maps etiketleme aracı — komut satırı")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="input/ dosyalarının yapısını ve kolon tespitini raporla")
    a.add_argument("files", nargs="*")
    a.add_argument("--json", action="store_true")
    a.add_argument("--no-cache", action="store_true")
    a.set_defaults(fn=cmd_analyze)

    b = sub.add_parser("prefetch", help="Bekleyen mekanların ekran görüntülerini toplu hazırla")
    b.add_argument("--flow", choices=["edit", "label"], default="edit")
    b.add_argument("--file")
    b.add_argument("--start-index", type=int, default=0)
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--max-concurrency", type=int, default=0)
    b.add_argument("--max-rate", type=float, default=0, help="dakikada en fazla sayfa açılışı (tavan)")
    b.add_argument("--retry-failed", action="store_true", help="geçici hatayla kalanları tekrar dene")
    b.add_argument("--report-every", type=float, default=5.0)
    b.set_defaults(fn=cmd_prefetch)

    c = sub.add_parser("failures", help="Başarısız yakalamaları listele")
    c.add_argument("--limit", type=int, default=50)
    c.set_defaults(fn=cmd_failures)

    d = sub.add_parser("prune", help="Önbellek temizliği (varsayılan: deneme modu)")
    d.add_argument("--orphans", action="store_true")
    d.add_argument("--older-than-hours", type=float, default=24.0)
    d.add_argument("--apply", action="store_true")
    d.set_defaults(fn=cmd_prune)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
