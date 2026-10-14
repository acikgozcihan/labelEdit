"""
Proje V1 — Veri Yönetimi (DataManager)
Excel/CSV dosyasını okur, 'Cihan Kategori' sütununu günceller.
SOLID: Single Responsibility — sadece veri I/O işlemleri burada.
"""
import pandas as pd
import openpyxl
import logging
from pathlib import Path
from typing import Optional

from config import (
    find_label_input_file,
    URL_COLUMN,
    CATEGORY_COLUMN,
    NAME_COLUMN,
    EXTRA_COLUMNS,
)

logger = logging.getLogger(__name__)


class DataManager:
    """
    Excel/CSV veri kaynağını yöneten sınıf.
    - İşlenmemiş (Cihan Kategori = boş) satırları döndürür.
    - Her kayıt sonrası dosyaya anlık yazar.
    """

    def __init__(self) -> None:
        self._file_path: Path = find_label_input_file()
        self._df: pd.DataFrame = pd.DataFrame()
        self._pending_indices: list[int] = []  # Orijinal DataFrame index'leri
        self._workbook: Optional[openpyxl.Workbook] = None
        self._sheet_name: Optional[str] = None
        self._category_col_letter: Optional[str] = None
        self._extra_col_letters: dict[str, str] = {}  # {sütun_adı: kolon_harfi}
        self._load()

    # ── Yükleme ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Dosyayı yükle, işlenmemiş satırları filtrele, openpyxl workbook'u hazırla."""
        ext = self._file_path.suffix.lower()

        if ext in (".xlsx", ".xls"):
            self._sheet_name = self._find_data_sheet_name()
            self._df = pd.read_excel(
                self._file_path,
                sheet_name=self._sheet_name,
                engine="openpyxl",
            )
        elif ext == ".csv":
            self._df = pd.read_csv(self._file_path, encoding="utf-8-sig")
        else:
            raise ValueError(f"Desteklenmeyen dosya formatı: {ext}")

        logger.info(f"Dosya yüklendi: {self._file_path} — {len(self._df)} satır")

        # URL ve kategori sütunlarını doğrula
        if URL_COLUMN not in self._df.columns:
            raise KeyError(f"'{URL_COLUMN}' sütunu bulunamadı. Mevcut sütunlar: {list(self._df.columns)}")

        if CATEGORY_COLUMN not in self._df.columns:
            # Sütun yoksa oluştur
            self._df[CATEGORY_COLUMN] = None
            logger.warning(f"'{CATEGORY_COLUMN}' sütunu bulunamadı, yeni sütun oluşturuldu.")

        # Ek sütunları kontrol et / oluştur
        for col_name in EXTRA_COLUMNS:
            if col_name not in self._df.columns:
                self._df[col_name] = None
                logger.warning(f"'{col_name}' sütunu bulunamadı, yeni sütun oluşturuldu.")

        # İşlenmemiş satırları bul:
        # 4 kolondan (Cihan Kategori + ek sütunlar) HEPSİ boşsa → pending
        # Herhangi biri doluysa → işlenmiş kabul edilir
        all_label_cols = [CATEGORY_COLUMN] + list(EXTRA_COLUMNS)

        # Excel'de tamamen boş kalan sütunlar pandas tarafından float64 olarak
        # algılanabilir. Etiket alanları serbest metin olduğundan tümünü object
        # tipine çevirerek sayı, metin ve sembol girişlerini güvenle destekle.
        for col_name in all_label_cols:
            self._df[col_name] = self._df[col_name].astype("object")

        def _is_empty(series: pd.Series) -> pd.Series:
            return series.isna() | (series.astype(str).str.strip() == "")

        all_empty_mask = pd.DataFrame({
            col: _is_empty(self._df[col]) for col in all_label_cols
        }).all(axis=1)

        has_url = self._df[URL_COLUMN].notna() & (self._df[URL_COLUMN].astype(str).str.strip() != "")

        mask = all_empty_mask & has_url
        self._pending_indices = self._df[mask].index.tolist()
        logger.info(f"İşlenecek satır sayısı: {len(self._pending_indices)}")

        # openpyxl workbook'u hazırla (xlsx ise, anlık yazma için)
        if ext in (".xlsx", ".xls"):
            self._workbook = openpyxl.load_workbook(self._file_path)
            self._prepare_category_column()

    def _find_data_sheet_name(self) -> str:
        """URL sütununu içeren tek Excel sayfasını bulur."""
        with pd.ExcelFile(self._file_path, engine="openpyxl") as excel_file:
            matching_sheets: list[str] = []
            columns_by_sheet: dict[str, list[str]] = {}

            for sheet_name in excel_file.sheet_names:
                columns = pd.read_excel(excel_file, sheet_name=sheet_name, nrows=0).columns.tolist()
                columns_by_sheet[sheet_name] = [str(column) for column in columns]
                if URL_COLUMN in columns:
                    matching_sheets.append(sheet_name)

        if len(matching_sheets) == 1:
            sheet_name = matching_sheets[0]
            logger.info(f"Veri sayfası seçildi: {sheet_name}")
            return sheet_name

        if not matching_sheets:
            details = "; ".join(
                f"{sheet_name}: {columns}" for sheet_name, columns in columns_by_sheet.items()
            )
            raise KeyError(
                f"'{URL_COLUMN}' sütununu içeren bir sayfa bulunamadı. Sayfa sütunları: {details}"
            )

        raise ValueError(
            f"'{URL_COLUMN}' sütununu içeren birden fazla sayfa bulundu: {matching_sheets}. "
            "Doğru veri sayfası tek olmalıdır."
        )

    def _prepare_category_column(self) -> None:
        """Kategori ve ek sütunların Excel'deki kolon harflerini bulur."""
        if not self._workbook or not self._sheet_name:
            raise RuntimeError("Excel çalışma kitabı veya veri sayfası hazır değil.")

        ws = self._workbook[self._sheet_name]
        headers = {cell.value: cell.column_letter for cell in ws[1]}

        # Ana kategori sütunu
        if CATEGORY_COLUMN in headers:
            self._category_col_letter = headers[CATEGORY_COLUMN]
        else:
            new_col = ws.max_column + 1
            ws.cell(row=1, column=new_col, value=CATEGORY_COLUMN)
            self._category_col_letter = openpyxl.utils.get_column_letter(new_col)
        logger.info(f"Kategori sütunu: {self._category_col_letter}")

        # Ek sütunlar
        for col_name in EXTRA_COLUMNS:
            if col_name in headers:
                self._extra_col_letters[col_name] = headers[col_name]
            else:
                new_col = ws.max_column + 1
                ws.cell(row=1, column=new_col, value=col_name)
                self._extra_col_letters[col_name] = openpyxl.utils.get_column_letter(new_col)
            logger.info(f"Ek sütun '{col_name}': {self._extra_col_letters[col_name]}")

        self._workbook.save(self._file_path)

    # ── Public API ───────────────────────────────────────────────────────────

    def get_pending_count(self) -> int:
        """İşlenmemiş satır sayısını döndürür."""
        return len(self._pending_indices)

    def get_total_count(self) -> int:
        """Toplam satır sayısı."""
        return len(self._df)

    def get_record(self, position: int) -> Optional[dict]:
        """
        Belirli bir konumdaki (0-indexed) işlenmemiş kaydı döndürür.
        Returns: {row_index, url, name, position, total}
        """
        if position >= len(self._pending_indices):
            return None

        df_index = self._pending_indices[position]
        row = self._df.loc[df_index]

        name = str(row.get(NAME_COLUMN, "")) if NAME_COLUMN in self._df.columns else ""

        return {
            "row_index": int(df_index),
            "url": str(row[URL_COLUMN]).strip(),
            "name": name,
            "position": position,
            "total": len(self._pending_indices),
        }

    def get_urls_from(self, start_position: int, count: int) -> list[dict]:
        """start_position'dan itibaren count adet kayıt döndürür (buffer için)."""
        results = []
        for pos in range(start_position, min(start_position + count, len(self._pending_indices))):
            record = self.get_record(pos)
            if record:
                results.append(record)
        return results

    def get_pending_records_from_index(self, start_index: int) -> list[dict]:
        """Veri indeksinden itibaren işlenmemiş kayıtları, oturum sırasıyla döndürür."""
        if start_index < 0:
            raise ValueError("Başlangıç indeksi 0 veya daha büyük olmalıdır.")

        start_position = next(
            (
                position
                for position, df_index in enumerate(self._pending_indices)
                if int(df_index) >= start_index
            ),
            len(self._pending_indices),
        )
        records = self.get_urls_from(start_position, len(self._pending_indices))

        # İlerleme göstergesi seçilen başlangıç noktasına göre hesaplanır.
        for position, record in enumerate(records):
            record["position"] = position
            record["total"] = len(records)

        return records

    def save_category(self, row_index: int, category: str, extra_fields: Optional[dict] = None) -> None:
        """
        Verilen orijinal DataFrame index'i için kategoriyi ve ek alanları dosyaya yazar.
        xlsx: openpyxl ile anlık satır güncelleme
        csv : pandas ile tam dosya yeniden yazma
        extra_fields: {"Çıkarılacak Noktalar": "...", "Açık Kapalı Kontrol": "...", "Pasif": "..."}
        """
        ext = self._file_path.suffix.lower()

        if ext in (".xlsx", ".xls") and self._workbook:
            self._save_to_xlsx(row_index, category, extra_fields)
        else:
            self._save_to_csv(row_index, category, extra_fields)

        # DataFrame'i de tam olarak güncelle. Böylece kullanıcı "Geri" ile
        # döndüğünde önceki bir değeri silebilir veya düzeltebilir.
        self._df.at[row_index, CATEGORY_COLUMN] = category or None
        for col_name in EXTRA_COLUMNS:
            value = (extra_fields or {}).get(col_name)
            self._df.at[row_index, col_name] = value or None

        logger.info(f"Kaydedildi — satır {row_index}: kategori='{category}', ek alanlar={extra_fields}")

    def _save_to_xlsx(self, row_index: int, category: str, extra_fields: Optional[dict] = None) -> None:
        """openpyxl ile ilgili hücreleri güncelle (sadece dolu alanlar)."""
        if not self._workbook or not self._sheet_name:
            raise RuntimeError("Excel çalışma kitabı veya veri sayfası hazır değil.")

        ws = self._workbook[self._sheet_name]
        excel_row = row_index + 2  # Excel satırı = DataFrame index + 2 (1-indexed + başlık satırı)

        # Satırın tüm etiket alanlarını değiştir; boş bırakılan eski değerler
        # de temizlenir.
        ws[f"{self._category_col_letter}{excel_row}"] = category or None
        for col_name in EXTRA_COLUMNS:
            col_letter = self._extra_col_letters.get(col_name)
            if col_letter:
                ws[f"{col_letter}{excel_row}"] = (extra_fields or {}).get(col_name) or None

        self._workbook.save(self._file_path)

    def _save_to_csv(self, row_index: int, category: str, extra_fields: Optional[dict] = None) -> None:
        """CSV için tam dosyayı yeniden yaz (sadece dolu alanlar)."""
        self._df.at[row_index, CATEGORY_COLUMN] = category or None
        for col_name in EXTRA_COLUMNS:
            self._df.at[row_index, col_name] = (extra_fields or {}).get(col_name) or None
        self._df.to_csv(self._file_path, index=False, encoding="utf-8-sig")
