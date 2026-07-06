/**
 * WindowManager - apertura finestre browser dedicate for viste esterne.
 */
const WindowManager = (() => {
  const DEFAULTS = {
    width: 1100,
    height: 720,
  };

  function open(url, name, options = {}) {
    if (!url) return null;

    const width = Number(options.width || DEFAULTS.width);
    const height = Number(options.height || DEFAULTS.height);
    const left = Math.max(0, Math.round((window.screenX || 0) + ((window.outerWidth || screen.width) - width) / 2));
    const top = Math.max(0, Math.round((window.screenY || 0) + ((window.outerHeight || screen.height) - height) / 2));
    const features = [
      'popup=yes',
      `width=${width}`,
      `height=${height}`,
      `left=${left}`,
      `top=${top}`,
      'resizable=yes',
      'scrollbars=yes',
    ].join(',');

    const win = window.open(url, name || '_blank', features);
    if (win && win.focus) win.focus();
    return win;
  }

  return { open };
})();
