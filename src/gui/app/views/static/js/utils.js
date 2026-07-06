/**
 * Shared utility functions.
 */

function sanitizeName(label) {
  return label.trim().replace(/[^a-zA-Z0-9_-]/g, '_') || 'node';
}

function generateId() {
  return Math.random().toString(36).slice(2, 9);
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.classList.add('show'), 10);
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 400);
  }, 3000);
}

function showModal(title, content, buttons = []) {
  const overlay = document.getElementById('modal-overlay');
  const modalTitle = document.getElementById('modal-title');
  const modalBody  = document.getElementById('modal-body');
  const modalFoot  = document.getElementById('modal-footer');
  const modal      = modalBody.closest('.modal');

  if (modal) modal.className = 'modal';
  modalTitle.textContent = title;
  if (typeof content === 'string') {
    modalBody.innerHTML = content;
  } else {
    modalBody.innerHTML = '';
    modalBody.appendChild(content);
    if (modal && content.dataset && content.dataset.modalSize) {
      modal.classList.add(`modal-${content.dataset.modalSize}`);
    }
  }
  modalFoot.innerHTML = '';
  buttons.forEach(btn => {
    const el = document.createElement('button');
    el.className = `btn ${btn.class || 'btn-secondary'}`;
    el.textContent = btn.label;
    el.onclick = () => { hideModal(); btn.action && btn.action(); };
    modalFoot.appendChild(el);
  });
  overlay.classList.add('active');
}

function hideModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

// Le mappe vendor-color / kind-label / kind-icon vivono in DeviceCatalog
// (device_catalog.js) che le legge da /config/devices.json. Qui esponiamo
// dei thin wrappers for tenere i call-site esistenti compatti.
function vendorColor(vendor) { return DeviceCatalog.vendorColor(vendor); }
function kindLabel(kind)     { return DeviceCatalog.kindLabel(kind); }
function _kindIcon(kind)     { return DeviceCatalog.kindIcon(kind); }
