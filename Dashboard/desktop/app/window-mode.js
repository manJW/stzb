'use strict';
const {windowBounds, compactWindowBounds} = require('./core');

// Reuse the same BrowserWindow and map WebContents, preserving focus and collection.
function createWindowMode({window, screen, saved = {}, selectMap, changed}) {
  let compact = false, switching = false;
  let full = {...window.getNormalBounds(),maximized:Boolean(saved.maximized)};
  let small = {...saved.compactBounds};
  const area = bounds => screen.getDisplayMatching(bounds).workArea;
  function setPinned(enabled) {
    window.setAlwaysOnTop(enabled);
    // Electron's default Windows level moves behind the taskbar. When the
    // taskbar itself is not topmost this can also remove our native topmost flag.
    if (enabled && !window.isAlwaysOnTop()) {
      window.setAlwaysOnTop(false);
      window.setAlwaysOnTop(true,'pop-up-menu');
    }
  }
  function apply(enabled) {
    if (window.isMaximized()) window.unmaximize();
    compact = enabled;
    window.setMinimumSize(...(enabled ? [360,420] : [1080,680]));
    window.setMaximizable(!enabled);
    window.setTitleBarOverlay({height:enabled?31:43});
    window.setBounds(enabled ? compactWindowBounds(small,area(full)) : windowBounds(full,area(full)));
    if (!enabled && full.maximized) window.maximize();
    // Overlay/style and bounds changes can clear the native topmost flag on Windows.
    setPinned(enabled);
    changed({compact});
  }
  return {
    get compact() { return compact; },
    ensurePinned() {
      if (!compact || window.isDestroyed() || window.isMinimized()) return;
      if (!window.isAlwaysOnTop()) setPinned(true);
    },
    initialize() { if (saved.compact === true) apply(true); },
    snapshot() {
      if (compact) small = window.getNormalBounds();
      else full = {...window.getNormalBounds(),maximized:window.isMaximized()};
      return {...full,compact,compactBounds:small};
    },
    async set(enabled) {
      if (switching || enabled === compact || window.isDestroyed()) return;
      switching = true;
      try {
        if (enabled) await selectMap();
        if (window.isDestroyed()) return;
        if (compact) small = window.getNormalBounds();
        else full = {...window.getNormalBounds(),maximized:window.isMaximized()};
        if (window.isMinimized()) window.restore();
        apply(enabled);
      } finally { switching = false; }
    },
  };
}
module.exports = {createWindowMode};
