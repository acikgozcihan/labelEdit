/**
 * Maps Düzenleyici — Edit Flow Frontend
 * SOLID Prensiplerine Uygun Modüler Mimari:
 *   - EditStateManager  : Uygulama durumu ve geçmiş takibi (backStack)
 *   - EditApiClient     : Backend API haberleşmesi (Action, Undo, Screenshot)
 *   - EditUIController  : DOM güncellemeleri ve kullanıcı arayüzü
 *   - EditKeyboardHandler: Klavye kısayolları (Sil, Atla, Tut, Geri, Harita)
 *   - ToastService      : Şık bildirimler
 *   - EditApp           : Ana orkestrasyon
 */
'use strict';

/* ────────────────────────────────────────────────────────────────────────────
   EditStateManager
   ──────────────────────────────────────────────────────────────────────────── */
class EditStateManager {
  constructor() {
    this._state = {
      isStarted:       false,
      isLoading:       false,
      currentRowIndex: null,
      currentName:     '',
      currentUrl:      '',
      currentPosition: 0,
      totalPending:    0,
      deletedCount:    0,
      keptCount:       0,
      skippedCount:    0,
      queueSize:       0,
      currentItem:     null,
      nextName:        '',
      sourceCompleted: false,
      backStack:       [], // Geri gitme için önceki kayıtlar
    };
  }
  get(key)        { return this._state[key]; }
  set(key, value) { this._state[key] = value; }
  update(partial) { Object.assign(this._state, partial); }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditApiClient
   ──────────────────────────────────────────────────────────────────────────── */
class EditApiClient {
  constructor(baseUrl = '') { this._base = baseUrl; }

  async startSession(startIndex) {
    const res = await fetch(`${this._base}/api/edit/session/start`, {
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
    const res = await fetch(`${this._base}/api/edit/screenshot/next`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async sendAction(rowIndex, action) {
    const res = await fetch(`${this._base}/api/edit/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_index: rowIndex, action }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async undoAction(rowIndex) {
    const res = await fetch(`${this._base}/api/edit/action/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_index: rowIndex }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async stopSession() {
    return fetch(`${this._base}/api/edit/session/stop`, { method: 'POST' });
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   ToastService
   ──────────────────────────────────────────────────────────────────────────── */
class ToastService {
  constructor(containerId) { this._container = document.getElementById(containerId); }
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

/* ────────────────────────────────────────────────────────────────────────────
   EditUIController
   ──────────────────────────────────────────────────────────────────────────── */
class EditUIController {
  constructor() {
    this.startScreen        = document.getElementById('start-screen');
    this.startBtn           = document.getElementById('start-btn');
    this.startIndexInput    = document.getElementById('start-index-input');
    this.loadingOverlay     = document.getElementById('loading-overlay');
    this.loadingText        = document.getElementById('loading-text');
    this.screenshotImg      = document.getElementById('screenshot-img');
    this.errorBadge         = document.getElementById('error-badge');
    this.completedScreen    = document.getElementById('completed-screen');
    this.completedSub       = document.getElementById('completed-sub');

    // Header
    this.progressFill       = document.getElementById('progress-bar-fill');
    this.progressText       = document.getElementById('progress-text');
    this.bufferDots         = document.querySelectorAll('.buffer-dot');
    this.backBtn            = document.getElementById('back-btn');
    this.openUrlBtn         = document.getElementById('open-url-btn');

    // Info panel
    this.businessName       = document.getElementById('business-name');
    this.urlPreview         = document.getElementById('url-preview');
    this.nextBusinessName   = document.getElementById('next-business-name');
    this.kontrolValueText   = document.getElementById('kontrol-value-text');
    this.statCurrent        = document.getElementById('stat-current');
    this.statRemaining      = document.getElementById('stat-remaining');
    this.statDeleted        = document.getElementById('stat-deleted');
    this.statSkipped        = document.getElementById('stat-skipped');
    this.statKept           = document.getElementById('stat-kept');
    this.historyList        = document.getElementById('history-list');

    // Action buttons
    this.deleteBtn          = document.getElementById('delete-btn');
    this.skipBtn            = document.getElementById('skip-btn');
    this.keepBtn            = document.getElementById('keep-btn');
    this.actionHint         = document.getElementById('action-hint');
  }

  /* -- Oturum Başlangıcı */
  showStartLoading() {
    this.startBtn.disabled = true;
    this.startIndexInput.disabled = true;
    this.startBtn.innerHTML = '<span class="spinner" style="width:18px;height:18px;border-width:2px"></span> Başlatılıyor...';
  }
  resetStartButton() {
    this.startBtn.disabled = false;
    this.startIndexInput.disabled = false;
    this.startBtn.innerHTML = '<span>&#9654;</span> Oturumu Başlat';
  }
  getStartIndex() {
    const value = this.startIndexInput.value.trim();
    if (value === '') return 0;
    const startIndex = Number(value);
    return Number.isInteger(startIndex) && startIndex >= 0 ? startIndex : null;
  }
  hideStartScreen() { this.startScreen.classList.add('hidden'); }

  activateUI() {
    this.deleteBtn.disabled  = false;
    this.skipBtn.disabled    = false;
    this.keepBtn.disabled    = false;
    this.openUrlBtn.disabled = false;
    this.actionHint.textContent = 'Delete / ← = Sil  |  Space / ↑ = Atla  |  Enter / → = Tut  |  Alt + ← = Geri';
  }

  setBackEnabled(enabled) {
    if (this.backBtn) this.backBtn.disabled = !enabled;
  }

  /* -- Screenshot Yükleme */
  showLoading(text = 'Harita yükleniyor...') {
    this.loadingOverlay.classList.remove('hidden');
    this.loadingText.textContent = text;
    this.screenshotImg.classList.remove('loaded');
    this.screenshotImg.src = '';
    this.errorBadge.style.display = 'none';
  }
  hideLoading() { this.loadingOverlay.classList.add('hidden'); }

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
      this.screenshotImg.src = fileUrl + '?t=' + Date.now();
    } else {
      this.hideLoading();
      this._showErrorBadge(errorMsg || 'Screenshot alınamadı');
    }
  }
  _showErrorBadge(msg) {
    this.errorBadge.textContent = '⚠ ' + msg;
    this.errorBadge.style.display = 'block';
  }

  /* -- Bilgi Paneli */
  updateInfo(data) {
    this.businessName.textContent = data.name || '(İsim yok)';
    const shortUrl = data.url ? data.url.replace('https://www.google.com/maps/place/', '').slice(0, 60) : '—';
    this.urlPreview.textContent = shortUrl + '...';
    this.urlPreview.title       = data.url || '';
    this.urlPreview.dataset.url = data.url || '';
    this.kontrolValueText.textContent = data.kontrol_value || '—';

    // Sıradaki mekan adı
    if (this.nextBusinessName) {
      this.nextBusinessName.textContent = data.next_name || '(Son kayıt veya yok)';
      this.nextBusinessName.title = data.next_name || '';
    }

    const pos       = (data.position ?? 0) + 1;
    const total     = data.total ?? 0;
    const remaining = total - pos;

    this.statCurrent.textContent   = pos.toLocaleString('tr-TR');
    this.statRemaining.textContent = Math.max(0, remaining).toLocaleString('tr-TR');
    this.statDeleted.textContent   = (data.deletedCount ?? 0).toLocaleString('tr-TR');
    if (this.statSkipped) this.statSkipped.textContent = (data.skippedCount ?? 0).toLocaleString('tr-TR');
    this.statKept.textContent      = (data.keptCount ?? 0).toLocaleString('tr-TR');

    const pct = total > 0 ? Math.round((pos / total) * 100) : 0;
    this.progressFill.style.width = pct + '%';
    this.progressText.textContent = `${pos} / ${total}`;
  }

  /* -- Buffer Göstergesi */
  updateBuffer(queueSize) {
    const filled = Math.min(queueSize, 10);
    this.bufferDots.forEach((dot, i) => {
      dot.className = 'buffer-dot';
      if (i < filled) dot.classList.add(filled >= 5 ? 'filled' : 'half');
    });
  }

  /* -- İşlem durumu */
  setActionsLoading(loading) {
    this.deleteBtn.disabled = loading;
    this.skipBtn.disabled   = loading;
    this.keepBtn.disabled   = loading;
    if (loading) {
      this.deleteBtn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>';
      this.skipBtn.innerHTML   = '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>';
      this.keepBtn.innerHTML   = '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>';
    } else {
      this.deleteBtn.innerHTML = '&#128465; Sil';
      this.skipBtn.innerHTML   = '&#9197; Atla';
      this.keepBtn.innerHTML   = '&#10003; Tut';
    }
  }

  /* -- Geçmiş */
  addHistory(name, action) {
    const empty = this.historyList.querySelector('p');
    if (empty) empty.remove();

    const item = document.createElement('div');
    item.className = 'edit-history-item';
    let actionClass = 'kept';
    let actionLabel = '✓ Tutuldu';
    if (action === 'delete') {
      actionClass = 'deleted';
      actionLabel = '🗑 Silindi';
    } else if (action === 'skip') {
      actionClass = 'skipped';
      actionLabel = '⏭ Atlandı';
    }

    item.innerHTML = `
      <div class="edit-history-name" title="${this._esc(name)}">${this._esc(name)}</div>
      <div class="edit-history-action ${actionClass}">${actionLabel}</div>
    `;
    this.historyList.insertBefore(item, this.historyList.firstChild);
    const items = this.historyList.querySelectorAll('.edit-history-item');
    if (items.length > 20) items[items.length - 1].remove();
  }

  /* -- Tamamlandı */
  showCompleted(deletedCount, keptCount, skippedCount) {
    this.completedSub.textContent =
      `${deletedCount} silindi, ${skippedCount || 0} atlandı, ${keptCount} tutuldu.`;
    this.completedScreen.classList.add('visible');
    this.deleteBtn.disabled  = true;
    this.skipBtn.disabled    = true;
    this.keepBtn.disabled    = true;
    this.openUrlBtn.disabled = true;
    if (this.backBtn) this.backBtn.disabled = true;
  }

  _esc(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditKeyboardHandler
   ──────────────────────────────────────────────────────────────────────────── */
class EditKeyboardHandler {
  constructor(onDelete, onSkip, onKeep, onBack, onOpenUrl) {
    this._onDelete  = onDelete;
    this._onSkip    = onSkip;
    this._onKeep    = onKeep;
    this._onBack    = onBack;
    this._onOpenUrl = onOpenUrl;
    this._enabled   = false;
  }
  enable()  { this._enabled = true; }
  disable() { this._enabled = false; }

  attach() {
    document.addEventListener('keydown', (e) => {
      if (!this._enabled) return;

      const target = e.target;
      const isInput = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA');

      // Geri Dön: Alt + Sol Ok, Backspace (input dışında) veya Ctrl + Z
      if ((e.altKey && e.key === 'ArrowLeft') || (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) || (!isInput && e.key === 'Backspace')) {
        e.preventDefault();
        this._onBack();
        return;
      }

      if (isInput) return;

      // Delete veya Sol Ok -> Sil
      if ((e.key === 'Delete' || e.key === 'ArrowLeft') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onDelete();
        return;
      }

      // Space, Yukarı Ok veya A -> Atla
      if ((e.key === ' ' || e.key === 'ArrowUp' || e.key === 'a' || e.key === 'A') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onSkip();
        return;
      }

      // Enter veya Sağ Ok -> Tut
      if ((e.key === 'Enter' || e.key === 'ArrowRight') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onKeep();
        return;
      }

      // Ctrl + O -> Haritayı aç
      if (e.key === 'o' && e.ctrlKey) {
        e.preventDefault();
        this._onOpenUrl();
      }
    });
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditApp — Ana Orkestrasyon
   ──────────────────────────────────────────────────────────────────────────── */
class EditApp {
  constructor() {
    this._state    = new EditStateManager();
    this._api      = new EditApiClient();
    this._ui       = new EditUIController();
    this._toast    = new ToastService('toast-container');
    this._keyboard = new EditKeyboardHandler(
      () => this._handleDelete(),
      () => this._handleSkip(),
      () => this._handleKeep(),
      () => this._handleBack(),
      () => this._handleOpenUrl(),
    );
    this._isTransitioning = false;
    this._isActing        = false;
  }

  init() {
    this._keyboard.attach();
    this._bindEvents();
  }

  _bindEvents() {
    this._ui.startBtn.addEventListener('click', () => this._startSession());
    this._ui.deleteBtn.addEventListener('click', () => this._handleDelete());
    this._ui.skipBtn.addEventListener('click', () => this._handleSkip());
    this._ui.keepBtn.addEventListener('click', () => this._handleKeep());
    if (this._ui.backBtn) this._ui.backBtn.addEventListener('click', () => this._handleBack());
    this._ui.openUrlBtn.addEventListener('click', () => this._handleOpenUrl());
    this._ui.urlPreview.addEventListener('click', () => {
      const url = this._ui.urlPreview.dataset.url;
      if (url) navigator.clipboard.writeText(url).then(() => this._toast.success('URL kopyalandı!'));
    });
  }

  /* -- Oturum Başlat */
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
      this._state.update({ isStarted: true, totalPending: data.pending });
      this._ui.hideStartScreen();
      this._ui.activateUI();
      this._keyboard.enable();
      this._toast.success(`İndeks ${data.start_index}'den itibaren ${data.pending} kayıt işlenecek`);
      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Hata: ${err.message}`);
      this._ui.resetStartButton();
    }
  }

  /* -- Sonraki Screenshot */
  async _loadNextScreenshot() {
    if (this._isTransitioning) return;
    this._isTransitioning = true;
    this._ui.showLoading();
    this._ui.setActionsLoading(true);

    try {
      const data = await this._api.getNextScreenshot();
      if (data.status === 'completed') {
        this._state.set('sourceCompleted', true);
        this._ui.showCompleted(
          this._state.get('deletedCount'),
          this._state.get('keptCount'),
          this._state.get('skippedCount')
        );
        return;
      }
      this._showItem(data);
    } catch (err) {
      this._toast.error(`Screenshot alınamadı: ${err.message}`);
      this._ui.hideLoading();
      this._ui.setActionsLoading(false);
    } finally {
      this._isTransitioning = false;
    }
  }

  _showItem(item) {
    if (!item) return;
    this._state.update({
      currentItem:     item,
      currentRowIndex: item.row_index,
      currentName:     item.name,
      currentUrl:      item.url,
      nextName:        item.next_name || '',
      queueSize:       item.queue_size ?? this._state.get('queueSize'),
    });
    this._ui.updateInfo({
      name:          item.name,
      next_name:     item.next_name || '',
      url:           item.url,
      kontrol_value: item.kontrol_value || '',
      position:      item.position,
      total:         item.total,
      deletedCount:  this._state.get('deletedCount'),
      skippedCount:  this._state.get('skippedCount'),
      keptCount:     this._state.get('keptCount'),
    });
    this._ui.updateBuffer(item.queue_size ?? this._state.get('queueSize'));
    this._ui.displayScreenshot(item.file_url, !!item.error, item.error);
    this._ui.setActionsLoading(false);
    this._ui.setBackEnabled(this._state.get('backStack').length > 0);
  }

  /* -- Sil */
  async _handleDelete() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      await this._api.sendAction(rowIndex, 'delete');
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'delete');
      this._state.set('deletedCount', this._state.get('deletedCount') + 1);
      this._ui.statDeleted.textContent = this._state.get('deletedCount').toLocaleString('tr-TR');
      this._toast.show(`🗑 Silindi: "${name}"`, 'error', 2200);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'delete' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Silme hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Atla */
  async _handleSkip() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      await this._api.sendAction(rowIndex, 'skip');
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'skip');
      this._state.set('skippedCount', this._state.get('skippedCount') + 1);
      if (this._ui.statSkipped) {
        this._ui.statSkipped.textContent = this._state.get('skippedCount').toLocaleString('tr-TR');
      }
      this._toast.info(`⏭ Atlandı: "${name}"`);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'skip' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Atlama hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Tut */
  async _handleKeep() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      await this._api.sendAction(rowIndex, 'keep');
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'keep');
      this._state.set('keptCount', this._state.get('keptCount') + 1);
      this._ui.statKept.textContent = this._state.get('keptCount').toLocaleString('tr-TR');
      this._toast.success(`✓ Tutuldu: "${name}"`);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'keep' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Tutma hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Geri Dön */
  async _handleBack() {
    if (this._isTransitioning || this._isActing) return;
    const backStack = this._state.get('backStack');
    if (backStack.length === 0) {
      this._toast.info('Geri dönülecek önceki kayıt yok.');
      return;
    }

    const { item, action } = backStack.pop();
    this._ui.setBackEnabled(backStack.length > 0);

    try {
      this._ui.setActionsLoading(true);
      // Backend'deki işlemi geri al
      await this._api.undoAction(item.row_index);

      // İstatistikleri düşür
      if (action === 'delete') {
        this._state.set('deletedCount', Math.max(0, this._state.get('deletedCount') - 1));
        this._ui.statDeleted.textContent = this._state.get('deletedCount').toLocaleString('tr-TR');
      } else if (action === 'skip') {
        this._state.set('skippedCount', Math.max(0, this._state.get('skippedCount') - 1));
        if (this._ui.statSkipped) {
          this._ui.statSkipped.textContent = this._state.get('skippedCount').toLocaleString('tr-TR');
        }
      } else if (action === 'keep') {
        this._state.set('keptCount', Math.max(0, this._state.get('keptCount') - 1));
        this._ui.statKept.textContent = this._state.get('keptCount').toLocaleString('tr-TR');
      }

      this._toast.info(`↩ Geri dönüldü: "${item.name}"`);
      this._showItem(item);
    } catch (err) {
      this._toast.error(`Geri alma hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    }
  }

  /* -- Haritayı Aç */
  _handleOpenUrl() {
    const url = this._state.get('currentUrl');
    if (url) window.open(url, '_blank', 'noopener,noreferrer');
  }
}

/* Bootstrap */
document.addEventListener('DOMContentLoaded', () => {
  const app = new EditApp();
  app.init();
});
