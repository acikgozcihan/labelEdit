"""Farklı Excel şablonlarıyla otomatik kolon tespiti testleri."""
import json

import pytest

from conftest import maps_url, write_xlsx
from data_model import ColumnDetectionError, ColumnRole
from excel_schema import analyze_file, get_schema, normalize_header


def _schema(path):
    return analyze_file(path, use_cache=False).schema


def test_normalize_header_variants():
    for h in ("Google Maps URL", "google_maps_url", "GoogleMapsURL", "GOOGLE-MAPS url", "google.maps.url"):
        assert normalize_header(h) == "google maps url", h
    assert normalize_header("İşletme Adı") == "isletme adi"
    assert normalize_header("KONTROL.1") == "kontrol"


def test_english_template_with_distractor_url_column(input_dir):
    rows = [("Place Name", "Website", "Google Maps URL", "Status", "Phone")]
    for i in range(40):
        rows.append((f"Cafe {i}", f"https://cafe{i}.com", maps_url(i), "done" if i % 3 == 0 else None, "+90 555"))
    p = write_xlsx(input_dir / "english.xlsx", {"Places": rows})
    s = _schema(p)
    assert s.column(ColumnRole.MAPS_URL) == "Google Maps URL"   # Website (URL ama Maps değil) seçilmedi
    assert s.column(ColumnRole.NAME) == "Place Name"
    assert s.column(ColumnRole.STATUS) == "Status"


def test_turkish_snake_and_spaced_variants(input_dir):
    rows = [("sıra", "işletme_adı", "harita_linki", "Kontrol Durumu", "kategori")]
    for i in range(30):
        rows.append((i, f"Berber {i}", maps_url(i), "ALAN DIŞI" if i % 2 else None, "Berber"))
    s = _schema(write_xlsx(input_dir / "tr.xlsx", {"Sayfa1": rows}))
    assert s.column(ColumnRole.MAPS_URL) == "harita_linki"
    assert s.column(ColumnRole.NAME) == "işletme_adı"
    assert s.column(ColumnRole.STATUS) == "Kontrol Durumu"


def test_german_camelcase_and_fuzzy_status(input_dir):
    rows = [("Bezeichnung", "KartenLink", "Prüfung")]
    for i in range(25):
        rows.append((f"Laden {i}", maps_url(i), "ok"))
    s = _schema(write_xlsx(input_dir / "de.xlsx", {"Daten": rows}))
    assert s.column(ColumnRole.MAPS_URL) == "KartenLink"
    assert s.column(ColumnRole.STATUS) == "Prüfung"
    assert s.column(ColumnRole.NAME) == "Bezeichnung"


def test_completely_unrelated_header_names_url_found_by_content(input_dir):
    rows = [("col_a", "col_b", "col_c")]
    for i in range(30):
        rows.append((f"x{i}", maps_url(i), i * 3))
    p = write_xlsx(input_dir / "weird.xlsx", {"S": rows})
    s = _schema(p)
    m = s.matches[ColumnRole.MAPS_URL.value]
    assert m.column == "col_b" and m.method == "content"
    # Durum kolonu yok → düzenle akışı için açıklayıcı hata (yanlış eşleştirme değil)
    with pytest.raises(ColumnDetectionError) as e:
        get_schema(p, ColumnRole.MAPS_URL, ColumnRole.STATUS)
    assert "Kontrol / durum" in str(e.value)


def test_header_not_on_first_row_and_summary_sheet_first(input_dir):
    summary = [("Ölçüm", "Değer"), ("Rapor tarihi", "13.10.2026"), ("Satır", 120)]
    data = [("Kozmetik taraması — rapor",), (None,), ("KONTROL", "isim", "maps_url", "place_id")]
    for i in range(60):
        data.append(("SINIRDA" if i % 4 == 0 else None, f"Mağaza {i}", maps_url(i), f"ChIJ{i:020d}"))
    p = write_xlsx(input_dir / "multi.xlsx", {"Özet": summary, "Kontrol özeti": [("KONTROL", "mekan")], "Mekanlar": data})
    prof = analyze_file(p, use_cache=False)
    s = prof.schema
    assert s.sheet == "Mekanlar"
    assert s.header_row == 3
    assert s.excel_row(0) == 4
    assert s.column(ColumnRole.STATUS) == "KONTROL"
    assert s.column(ColumnRole.PLACE_ID) == "place_id"


def test_short_links_and_partial_invalid_values_warn(input_dir):
    rows = [("Ad", "Link")]
    for i in range(20):
        url = f"https://maps.app.goo.gl/abc{i}" if i % 2 else f"goo.gl/maps/xyz{i}"
        rows.append((f"Yer {i}", url if i < 17 else "bilinmiyor"))
    s = _schema(write_xlsx(input_dir / "short.xlsx", {"S": rows}))
    assert s.column(ColumnRole.MAPS_URL) == "Link"
    assert any("geçerli Maps" in w for w in s.warnings)


def test_headerless_file(input_dir):
    rows = [(f"Yer {i}", maps_url(i)) for i in range(15)]
    s = _schema(write_xlsx(input_dir / "noheader.xlsx", {"S": rows}))
    assert s.header_row == 0
    assert s.column(ColumnRole.MAPS_URL) == "Kolon B"


def test_ambiguous_status_columns_raise_instead_of_guessing(input_dir):
    rows = [("isim", "url", "Kontrol", "Durum")]
    for i in range(20):
        rows.append((f"A{i}", maps_url(i), None, None))
    p = write_xlsx(input_dir / "ambig.xlsx", {"S": rows})
    s = _schema(p)
    assert ColumnRole.STATUS.value not in s.matches
    assert any("belirsiz" in e for e in s.errors)
    with pytest.raises(ColumnDetectionError):
        get_schema(p, ColumnRole.MAPS_URL, ColumnRole.STATUS)


def test_override_file_resolves_ambiguity(input_dir):
    import config
    rows = [("isim", "url", "Kontrol", "Durum")] + [(f"A{i}", maps_url(i), None, "x") for i in range(10)]
    p = write_xlsx(input_dir / "ambig2.xlsx", {"S": rows})
    config.COLUMN_OVERRIDES_FILE.write_text(json.dumps({"ambig2.xlsx": {"status": "Durum"}}), encoding="utf-8")
    try:
        s = get_schema(p, ColumnRole.MAPS_URL, ColumnRole.STATUS)
        assert s.column(ColumnRole.STATUS) == "Durum"
        assert s.matches["status"].method == "override"
    finally:
        config.COLUMN_OVERRIDES_FILE.unlink()


def test_no_maps_column_gives_clear_error(input_dir):
    rows = [("isim", "website")] + [(f"A{i}", f"https://a{i}.com") for i in range(10)]
    p = write_xlsx(input_dir / "nourl.xlsx", {"S": rows})
    with pytest.raises(ColumnDetectionError) as e:
        get_schema(p, ColumnRole.MAPS_URL)
    assert "Google Maps" in str(e.value)


def test_csv_semicolon(input_dir):
    p = input_dir / "semi.csv"
    lines = ["Name;Maps;State"] + [f"Shop {i};{maps_url(i)};{'ok' if i % 2 else ''}" for i in range(12)]
    p.write_text("\n".join(lines), encoding="utf-8")
    s = _schema(p)
    assert (s.column(ColumnRole.MAPS_URL), s.column(ColumnRole.NAME), s.column(ColumnRole.STATUS)) == \
        ("Maps", "Name", "State")


def test_cache_returns_same_result_fast(input_dir):
    import time
    rows = [("isim", "url")] + [(f"A{i}", maps_url(i)) for i in range(50)]
    p = write_xlsx(input_dir / "cache.xlsx", {"S": rows})
    a = analyze_file(p)
    t = time.perf_counter()
    b = analyze_file(p)
    assert time.perf_counter() - t < 0.05
    assert a.to_dict() == b.to_dict()
