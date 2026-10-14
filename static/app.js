/**
 * Proje V1 — Frontend Uygulama Mantığı
 * SOLID: Her sınıf tek bir sorumluluğa sahip
 *   - StateManager   : Uygulama durumu
 *   - ApiClient      : API iletişimi
 *   - UIController   : DOM güncellemeleri
 *   - KeyboardHandler: Klavye olayları
 *   - ToastService   : Bildirimler
 *   - App            : Orkestrasyon
 */

'use strict';

/* ════════════════════════════════════════════════════════════════════════════
   StateManager — Tek kaynak uygulama durumu
   ════════════════════════════════════════════════════════════════════════════ */
class StateManager {
  constructor() {
    this._state = {
      isStarted:       false,
      isLoading:       false,
      currentRowIndex: null,
      currentName:     '',
      currentUrl:      '',
      currentPosition: 0,
      totalPending:    0,
      totalAll:        0,
      doneCount:       0,
      queueSize:       0,
      history:         [],   // [{name, category}]
      currentItem:     null,
      backStack:       [],
      forwardStack:    [],
      deferredItems:   [],
      savedRows:       new Set(),
      countedRows:     new Set(),
      sourceCompleted: false,
    };
  }

  get(key)            { return this._state[key]; }
  set(key, value)     { this._state[key] = value; }
  update(partial)     { Object.assign(this._state, partial); }
  getAll()            { return { ...this._state }; }
}

/* ════════════════════════════════════════════════════════════════════════════
   ApiClient — Tüm backend iletişimi
   ════════════════════════════════════════════════════════════════════════════ */
class ApiClient {
  constructor(baseUrl = '') {
    this._base = baseUrl;
  }

  async startSession(startIndex) {
    const res = await fetch(`${this._base}/api/session/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_index: startIndex }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async getNextScreenshot() {
    const res = await fetch(`${this._base}/api/screenshot/next`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async saveLabel(rowIndex, category, extraFields = null) {
    const body = { row_index: rowIndex, category };
    if (extraFields) {
      body.extra_fields = extraFields;
    }
    const res = await fetch(`${this._base}/api/label`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async stopSession() {
    return fetch(`${this._base}/api/session/stop`, { method: 'POST' });
  }
}

/* ════════════════════════════════════════════════════════════════════════════
   ToastService — Bildirim sistemi
   ════════════════════════════════════════════════════════════════════════════ */
class ToastService {
  constructor(containerId) {
    this._container = document.getElementById(containerId);
  }

  show(message, type = 'info', duration = 2800) {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    this._container.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
  }

  success(msg) { this.show(msg, 'success'); }
  error(msg)   { this.show(msg, 'error'); }
  info(msg)    { this.show(msg, 'info'); }
}

/* ════════════════════════════════════════════════════════════════════════════
   UIController — Tüm DOM manipülasyonları
   ════════════════════════════════════════════════════════════════════════════ */
class UIController {
  constructor() {
    // Elementler
    this.startScreen    = document.getElementById('start-screen');
    this.startBtn       = document.getElementById('start-btn');
    this.startIndexInput = document.getElementById('start-index-input');
    this.loadingOverlay = document.getElementById('loading-overlay');
    this.loadingText    = document.getElementById('loading-text');
    this.screenshotImg  = document.getElementById('screenshot-img');
    this.errorBadge     = document.getElementById('error-badge');
    this.completedScreen= document.getElementById('completed-screen');
    this.completedSub   = document.getElementById('completed-sub');

    // Header
    this.progressFill   = document.getElementById('progress-bar-fill');
    this.progressText   = document.getElementById('progress-text');
    this.bufferDots     = document.querySelectorAll('.buffer-dot');
    this.openUrlBtn     = document.getElementById('open-url-btn');
    this.skipBtn        = document.getElementById('skip-btn');
    this.backBtn        = document.getElementById('back-btn');

    // Info panel
    this.businessName   = document.getElementById('business-name');
    this.urlPreview     = document.getElementById('url-preview');
    this.statCurrent    = document.getElementById('stat-current');
    this.statRemaining  = document.getElementById('stat-remaining');
    this.statDone       = document.getElementById('stat-done');
    this.statTotal      = document.getElementById('stat-total');
    this.historyList    = document.getElementById('history-list');
    this.deferredList   = document.getElementById('deferred-list');
    this.deferredCount  = document.getElementById('deferred-count');

    // Bottom bar
    this.categoryInput  = document.getElementById('category-input');
    this.inputSuffix    = document.getElementById('input-suffix');
    this.submitBtn      = document.getElementById('submit-btn');
    this.deferBtn       = document.getElementById('defer-btn');

    // Ek input alanları
    this.extraCikarilacak = document.getElementById('extra-cikarilacak');
    this.extraAcikKapali  = document.getElementById('extra-acik-kapali');
    this.extraPasif       = document.getElementById('extra-pasif');

    // Tüm inputları grupla (toplu enable/disable için)
    this._allInputs = [
      this.categoryInput,
      this.extraCikarilacak,
      this.extraAcikKapali,
      this.extraPasif,
    ];
  }

  /* ── Oturum Başlangıcı ─────────────────────────────────────────────────── */
  showStartLoading() {
    this.startBtn.disabled = true;
    this.startIndexInput.disabled = true;
    this.startBtn.innerHTML = '<span class="spinner" style="width:18px;height:18px;border-width:2px"></span> Başlatılıyor...';
  }

  resetStartButton() {
    this.startBtn.disabled = false;
    this.startIndexInput.disabled = false;
    this.startBtn.innerHTML = '<span>▶</span> Oturumu Başlat';
  }

  getStartIndex() {
    const value = this.startIndexInput.value.trim();
    if (value === '') return 0;

    const startIndex = Number(value);
    return Number.isInteger(startIndex) && startIndex >= 0 ? startIndex : null;
  }

  hideStartScreen() {
    this.startScreen.classList.add('hidden');
  }

  activateUI(data) {
    this._allInputs.forEach(input => input.disabled = false);
    this.submitBtn.disabled      = false;
    this.skipBtn.disabled        = false;
    this.deferBtn.disabled       = false;
    this.openUrlBtn.disabled     = false;
    this.categoryInput.focus();

    this.statTotal.textContent = data.pending.toLocaleString('tr-TR');
  }

  /* ── Screenshot Yükleme ────────────────────────────────────────────────── */
  showLoading(text = 'Screenshot hazırlanıyor...') {
    this.loadingOverlay.classList.remove('hidden');
    this.loadingText.textContent = text;
    this.screenshotImg.classList.remove('loaded');
    this.screenshotImg.src = '';
    this.errorBadge.style.display = 'none';
  }

  hideLoading() {
    this.loadingOverlay.classList.add('hidden');
  }

  displayScreenshot(fileUrl, hasError, errorMsg = '') {
    if (fileUrl && !hasError) {
      this.screenshotImg.onload = () => {
        this.screenshotImg.classList.add('loaded');
        this.hideLoading();
      };
      this.screenshotImg.onerror = () => {
        this.hideLoading();
        this._showErrorBadge('Görsel yüklenemedi');
      };
      this.screenshotImg.src = fileUrl + '?t=' + Date.now(); // cache-bust
    } else {
      this.hideLoading();
      this._showErrorBadge(errorMsg || 'Screenshot alınamadı');
    }
  }

  _showErrorBadge(msg) {
    this.errorBadge.textContent = '⚠ ' + msg;
    this.errorBadge.style.display = 'block';
  }

  /* ── Bilgi Paneli ──────────────────────────────────────────────────────── */
  updateInfo(data) {
    // İşletme adı
    this.businessName.textContent = data.name || '(İsim yok)';

    // URL preview
    const shortUrl = data.url ? data.url.replace('https://www.google.com/maps/place/', '').slice(0, 60) : '—';
    this.urlPreview.textContent   = shortUrl + '...';
    this.urlPreview.title         = data.url || '';
    this.urlPreview.dataset.url   = data.url || '';

    // İstatistikler
    const pos       = (data.position ?? 0) + 1;
    const total     = data.total ?? 0;
    const remaining = total - pos;

    this.statCurrent.textContent   = pos.toLocaleString('tr-TR');
    this.statRemaining.textContent = Math.max(0, remaining).toLocaleString('tr-TR');
    this.statDone.textContent      = (data.doneCount ?? 0).toLocaleString('tr-TR');

    // Progress bar
    const pct = total > 0 ? Math.round((pos / total) * 100) : 0;
    this.progressFill.style.width  = pct + '%';
    this.progressText.textContent  = `${pos} / ${total}`;
  }

  /* ── Buffer Göstergesi ─────────────────────────────────────────────────── */
  updateBuffer(queueSize) {
    const filled = Math.min(queueSize, 10);
    this.bufferDots.forEach((dot, i) => {
      dot.className = 'buffer-dot';
      if (i < filled) {
        dot.classList.add(filled >= 5 ? 'filled' : 'half');
      }
    });
  }

  /* ── Input'u Temizle / Hazırla ─────────────────────────────────────────── */
  clearInput() {
    this._allInputs.forEach(input => input.value = '');
    this.categoryInput.focus();
  }

  setInputValues(values = {}) {
    this.categoryInput.value = values.category || '';
    this.extraCikarilacak.value = values.extraFields?.['Çıkarılacak Noktalar'] || '';
    this.extraAcikKapali.value = values.extraFields?.['Açık Kapalı Kontrol'] || '';
    this.extraPasif.value = values.extraFields?.['Pasif'] || '';
    this.categoryInput.focus();
  }

  getInputValue() {
    return this.categoryInput.value.trim();
  }

  /** Ek alanların değerlerini döndürür (sadece dolu olanlar) */
  getExtraFields() {
    const fields = {};
    const cikarilacak = this.extraCikarilacak.value.trim();
    const acikKapali  = this.extraAcikKapali.value.trim();
    const pasif       = this.extraPasif.value.trim();

    if (cikarilacak) fields['Çıkarılacak Noktalar'] = cikarilacak;
    if (acikKapali)  fields['Açık Kapalı Kontrol']  = acikKapali;
    if (pasif)       fields['Pasif']                 = pasif;

    return Object.keys(fields).length > 0 ? fields : null;
  }

  setInputLoading(loading) {
    this._allInputs.forEach(input => input.disabled = loading);
    this.submitBtn.disabled = loading;
    this.deferBtn.disabled = loading;
    if (loading) {
      this.submitBtn.innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px"></span>';
    } else {
      this.submitBtn.innerHTML = 'Kaydet &amp; İlerle →';
    }
  }

  setBackEnabled(enabled) {
    this.backBtn.disabled = !enabled;
  }

  renderDeferred(items, currentRowIndex, onSelect) {
    this.deferredCount.textContent = items.length.toLocaleString('tr-TR');
    this.deferredList.innerHTML = '';

    if (items.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'empty-list-message';
      empty.textContent = 'Liste boş';
      this.deferredList.appendChild(empty);
      return;
    }

    items.forEach(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'deferred-item';
      if (item.row_index === currentRowIndex) button.classList.add('active');
      button.textContent = item.name || '(İsim yok)';
      button.title = item.name || '(İsim yok)';
      button.addEventListener('click', () => onSelect(item.row_index));
      this.deferredList.appendChild(button);
    });
  }

  /* ── Geçmiş ────────────────────────────────────────────────────────────── */
  addHistory(name, category) {
    // "Henüz kayıt yok" metnini kaldır
    const empty = this.historyList.querySelector('p');
    if (empty) empty.remove();

    const item = document.createElement('div');
    item.className = 'history-item';
    item.innerHTML = `
      <div class="history-item-name" title="${this._esc(name)}">${this._esc(name)}</div>
      <div class="history-item-cat">${this._esc(category)}</div>
    `;
    // En üste ekle
    this.historyList.insertBefore(item, this.historyList.firstChild);

    // Max 20 item tut
    const items = this.historyList.querySelectorAll('.history-item');
    if (items.length > 20) items[items.length - 1].remove();
  }

  /* ── Tamamlandı ────────────────────────────────────────────────────────── */
  showCompleted(doneCount) {
    this.completedSub.textContent = `${doneCount} kayıt etiketlendi ve Excel'e kaydedildi.`;
    this.completedScreen.classList.add('visible');
    this._allInputs.forEach(input => input.disabled = true);
    this.submitBtn.disabled       = true;
    this.deferBtn.disabled        = true;
    this.backBtn.disabled         = true;
    this.skipBtn.disabled         = true;
    this.openUrlBtn.disabled      = true;
  }

  _esc(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
}

/* ════════════════════════════════════════════════════════════════════════════
   KeyboardHandler — Klavye olaylarını yönetir
   ════════════════════════════════════════════════════════════════════════════ */
class KeyboardHandler {
  constructor(onSubmit, onSkip, onOpenUrl, onBack, onDefer) {
    this._onSubmit  = onSubmit;
    this._onSkip    = onSkip;
    this._onOpenUrl = onOpenUrl;
    this._onBack    = onBack;
    this._onDefer   = onDefer;
    this._enabled   = false;
  }

  enable()  { this._enabled = true; }
  disable() { this._enabled = false; }

  attach() {
    document.addEventListener('keydown', (e) => {
      if (!this._enabled) return;

      const tag = document.activeElement?.tagName?.toLowerCase();

      // Enter → Kaydet & İlerle (herhangi bir input'tayken)
      if (e.key === 'Enter' && tag === 'input' && !e.ctrlKey) {
        e.preventDefault();
        this._onSubmit();
        return;
      }

      // Sağ Ok → Kaydet & İlerle (sadece kategori input'undayken)
      if (e.key === 'ArrowRight' && document.activeElement?.id === 'category-input' && !e.ctrlKey) {
        e.preventDefault();
        this._onSubmit();
        return;
      }

      // Ctrl + Sağ Ok → Atla
      if (e.key === 'ArrowRight' && e.ctrlKey) {
        e.preventDefault();
        this._onSkip();
        return;
      }

      // Ctrl + O → Haritayı aç
      if (e.key === 'o' && e.ctrlKey) {
        e.preventDefault();
        this._onOpenUrl();
        return;
      }

      // Alt + Sol Ok → Önceki kayda dön
      if (e.key === 'ArrowLeft' && e.altKey) {
        e.preventDefault();
        this._onBack();
        return;
      }

      // Ctrl + D → Daha sonra işaretle
      if (e.key.toLowerCase() === 'd' && e.ctrlKey) {
        e.preventDefault();
        this._onDefer();
      }
    });
  }
}

/* ════════════════════════════════════════════════════════════════════════════
   App — Ana Orkestrasyon
   ════════════════════════════════════════════════════════════════════════════ */
class App {
  constructor() {
    this._state    = new StateManager();
    this._api      = new ApiClient();
    this._ui       = new UIController();
    this._toast    = new ToastService('toast-container');
    this._keyboard = new KeyboardHandler(
      () => this._handleSubmit(),
      () => this._handleSkip(),
      () => this._handleOpenUrl(),
      () => this._handleBack(),
      () => this._handleDefer(),
    );

    this._isTransitioning = false;  // Çift tıklama koruması
    this._isSaving = false;
  }

  init() {
    this._keyboard.attach();
    this._bindEvents();
  }

  /* ── Event Binding ──────────────────────────────────────────────────────── */
  _bindEvents() {
    // Başlat butonu
    this._ui.startBtn.addEventListener('click', () => this._startSession());

    // Kaydet butonu
    this._ui.submitBtn.addEventListener('click', () => this._handleSubmit());

    // Atla butonu
    this._ui.skipBtn.addEventListener('click', () => this._handleSkip());
    this._ui.backBtn.addEventListener('click', () => this._handleBack());
    this._ui.deferBtn.addEventListener('click', () => this._handleDefer());

    // URL kopyala
    this._ui.urlPreview.addEventListener('click', () => {
      const url = this._ui.urlPreview.dataset.url;
      if (url) {
        navigator.clipboard.writeText(url).then(() => this._toast.success('URL kopyalandı!'));
      }
    });

    // Harita aç
    this._ui.openUrlBtn.addEventListener('click', () => this._handleOpenUrl());

    this._renderDeferred();
  }

  /* ── Oturum Başlatma ────────────────────────────────────────────────────── */
  async _startSession() {
    const startIndex = this._ui.getStartIndex();
    if (startIndex === null) {
      this._toast.error('Başlangıç indeksi 0 veya daha büyük tam sayı olmalı.');
      this._ui.startIndexInput.focus();
      return;
    }

    this._ui.showStartLoading();
    try {
      const data = await this._api.startSession(startIndex);
      this._state.update({
        isStarted:    true,
        totalPending: data.pending,
        totalAll:     data.total,
      });

      this._ui.hideStartScreen();
      this._ui.activateUI(data);
      this._keyboard.enable();

      this._toast.success(`İndeks ${data.start_index}'den itibaren ${data.pending} kayıt işlenecek`);

      // İlk screenshot'ı yükle
      await this._loadNextScreenshot();

    } catch (err) {
      this._toast.error(`Hata: ${err.message}`);
      this._ui.resetStartButton();
    }
  }

  /* ── Sonraki Screenshot ─────────────────────────────────────────────────── */
  async _loadNextScreenshot() {
    if (this._isTransitioning) return;
    this._isTransitioning = true;

    this._ui.showLoading('Harita yükleniyor...');
    this._ui.setInputLoading(true);

    try {
      const data = await this._api.getNextScreenshot();

      if (data.status === 'completed') {
        this._state.set('sourceCompleted', true);
        const deferred = this._state.get('deferredItems');
        if (deferred.length > 0) {
          this._showItem(deferred[0]);
          this._toast.info('Ana liste tamamlandı. Daha sonra işaretlenecek kayıtlar açıldı.');
        } else {
          this._ui.showCompleted(this._state.get('doneCount'));
        }
        return;
      }

      this._showItem({
        ...data,
        category: '',
        extraFields: null,
      });

    } catch (err) {
      this._toast.error(`Screenshot alınamadı: ${err.message}`);
      this._ui.hideLoading();
      this._ui.setInputLoading(false);
    } finally {
      this._isTransitioning = false;
    }
  }

  _captureCurrentItem() {
    const item = this._state.get('currentItem');
    if (!item) return null;

    item.category = this._ui.getInputValue();
    item.extraFields = this._ui.getExtraFields();
    return item;
  }

  _showItem(item) {
    if (!item) return;

    this._state.update({
      currentItem:     item,
      currentRowIndex: item.row_index,
      currentName:     item.name,
      currentUrl:      item.url,
      queueSize:       item.queue_size ?? this._state.get('queueSize'),
    });

    this._ui.updateInfo({
      name:      item.name,
      url:       item.url,
      position:  item.position,
      total:     item.total,
      doneCount: this._state.get('doneCount'),
    });
    this._ui.updateBuffer(item.queue_size ?? this._state.get('queueSize'));
    this._ui.displayScreenshot(item.file_url, !!item.error, item.error);
    this._ui.setInputLoading(false);
    this._ui.setInputValues(item);
    this._ui.setBackEnabled(this._state.get('backStack').length > 0);
    this._renderDeferred();
  }

  async _advanceFromCurrent() {
    const current = this._captureCurrentItem();
    if (current) this._state.get('backStack').push(current);

    const forward = this._state.get('forwardStack');
    if (forward.length > 0) {
      this._showItem(forward.pop());
      return;
    }

    if (this._state.get('sourceCompleted')) {
      const deferred = this._state.get('deferredItems');
      if (deferred.length > 0) {
        this._showItem(deferred[0]);
      } else {
        this._ui.showCompleted(this._state.get('doneCount'));
      }
      return;
    }

    await this._loadNextScreenshot();
  }

  /* ── Kaydet & İlerle ────────────────────────────────────────────────────── */
  async _handleSubmit() {
    if (this._isTransitioning || this._isSaving) return;

    const category    = this._ui.getInputValue();
    const extraFields = this._ui.getExtraFields();

    // En az bir alan doldurulmalı
    if (!category && !extraFields) {
      this._toast.error('En az bir alan doldurulmalıdır!');
      this._ui.categoryInput.classList.add('shake');
      setTimeout(() => this._ui.categoryInput.classList.remove('shake'), 400);
      return;
    }

    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null) return;

    this._ui.setInputLoading(true);
    this._isSaving = true;

    try {
      await this._api.saveLabel(rowIndex, category, extraFields);

      const name = this._state.get('currentName');
      this._ui.addHistory(name, category);
      const savedRows = this._state.get('savedRows');
      const countedRows = this._state.get('countedRows');
      savedRows.add(rowIndex);
      if (!countedRows.has(rowIndex)) {
        countedRows.add(rowIndex);
        this._state.set('doneCount', this._state.get('doneCount') + 1);
      }
      this._ui.statDone.textContent = this._state.get('doneCount').toLocaleString('tr-TR');

      const current = this._state.get('currentItem');
      current.category = category;
      current.extraFields = extraFields;
      this._removeDeferred(rowIndex);

      const label = category || 'ek alanlar';
      this._toast.success(`✓ Kaydedildi: "${label}"`);

      await this._advanceFromCurrent();

    } catch (err) {
      this._toast.error(`Kaydetme hatası: ${err.message}`);
      this._ui.setInputLoading(false);
    } finally {
      this._isSaving = false;
    }
  }

  /* ── Atla (boş bırak) ───────────────────────────────────────────────────── */
  async _handleSkip() {
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || this._isTransitioning || this._isSaving) return;

    // Boş string ile kaydet (atlandı olarak işaretle)
    try {
      this._ui.setInputLoading(true);
      this._isSaving = true;
      await this._api.saveLabel(rowIndex, '(Atlandı)');
      const savedRows = this._state.get('savedRows');
      savedRows.add(rowIndex);
      const countedRows = this._state.get('countedRows');
      if (countedRows.delete(rowIndex)) {
        this._state.set('doneCount', Math.max(0, this._state.get('doneCount') - 1));
        this._ui.statDone.textContent = this._state.get('doneCount').toLocaleString('tr-TR');
      }
      const current = this._state.get('currentItem');
      current.category = '(Atlandı)';
      current.extraFields = null;
      this._removeDeferred(rowIndex);
      this._toast.info('Kayıt atlandı');
      await this._advanceFromCurrent();
    } catch (err) {
      this._toast.error(`Atlama hatası: ${err.message}`);
      this._ui.setInputLoading(false);
    } finally {
      this._isSaving = false;
    }
  }

  /* ── Geri / Daha Sonra ──────────────────────────────────────────────────── */
  _handleBack() {
    if (this._isTransitioning || this._isSaving) return;
    const back = this._state.get('backStack');
    if (back.length === 0) {
      this._toast.info('Geri dönülecek kayıt yok.');
      return;
    }

    const current = this._captureCurrentItem();
    if (current) this._state.get('forwardStack').push(current);
    this._showItem(back.pop());
  }

  async _handleDefer() {
    if (this._isTransitioning || this._isSaving) return;
    const current = this._captureCurrentItem();
    if (!current) return;

    const deferred = this._state.get('deferredItems');
    const alreadyDeferred = deferred.some(item => item.row_index === current.row_index);
    if (!alreadyDeferred) {
      deferred.push(current);
      this._toast.info('Kayıt “Daha Sonra İşaretlenecekler” listesine eklendi.');
    } else {
      this._toast.info('Bu kayıt zaten daha sonra listesinde.');
      if (this._state.get('forwardStack').length > 0) {
        await this._advanceFromCurrent();
      }
      return;
    }
    this._renderDeferred();
    await this._advanceFromCurrent();
  }

  _openDeferred(rowIndex) {
    if (this._isTransitioning || this._isSaving || rowIndex === this._state.get('currentRowIndex')) return;

    const target = this._state.get('deferredItems').find(item => item.row_index === rowIndex);
    if (!target) return;

    const current = this._captureCurrentItem();
    if (current) {
      const isSaved = this._state.get('savedRows').has(current.row_index);
      const deferred = this._state.get('deferredItems');
      if (!isSaved && !deferred.some(item => item.row_index === current.row_index)) {
        deferred.push(current);
      }
      this._state.get('backStack').push(current);
    }
    this._state.get('forwardStack').length = 0;
    this._showItem(target);
  }

  _removeDeferred(rowIndex) {
    const deferred = this._state.get('deferredItems');
    const index = deferred.findIndex(item => item.row_index === rowIndex);
    if (index >= 0) deferred.splice(index, 1);
    this._renderDeferred();
  }

  _renderDeferred() {
    this._ui.renderDeferred(
      this._state.get('deferredItems'),
      this._state.get('currentRowIndex'),
      rowIndex => this._openDeferred(rowIndex),
    );
  }

  /* ── Haritayı Aç ────────────────────────────────────────────────────────── */
  _handleOpenUrl() {
    const url = this._state.get('currentUrl');
    if (url) {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  }
}

/* ════════════════════════════════════════════════════════════════════════════
   Bootstrap
   ════════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  const app = new App();
  app.init();
});
