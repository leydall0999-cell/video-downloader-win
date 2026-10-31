// tests/test_frontend_isolation.mjs — 前端物理隔离守门测试（Phase 2）
//
// 锁定两条不变量，防止以后又被改回「前端又混在一起」：
//   1. index.html 不得无条件加载 desktop-app.js（必须仅 pywebview 环境才加载）。
//   2. app.js 必须把共享 helper 暴露到 window.VDL；desktop-app.js 行为正确且自门控。
//
// 运行：node tests/test_frontend_isolation.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const WEB = join(repoRoot, 'web');
const INDEX = join(WEB, 'index.html');
const APPJS = join(WEB, 'app.js');
const DESKTOP = join(WEB, 'js', 'desktop-app.js');

const indexHtml = readFileSync(INDEX, 'utf8');
const appJs = readFileSync(APPJS, 'utf8');
const desktopJs = readFileSync(DESKTOP, 'utf8');

// 在隔离作用域里执行 desktop-app.js，注入 fake window/document
function runDesktop(win, doc) {
  // eslint-disable-next-line no-new-func
  const fn = new Function('window', 'document', 'console', desktopJs);
  fn(win, doc, console);
}

function mkEl() {
  return {
    hidden: true, _h: {}, textContent: '', style: {}, disabled: false,
    appendChild() {}, addEventListener(ev, h) { this._h[ev] = h; },
  };
}

// desktop-app.js 的 initSyncButton 需要 document.createElement / document.body；
function mkDoc() {
  const doc = {
    _h: {}, body: mkEl(),
    createElement() { return mkEl(); },
    querySelector(sel) { return sel === 'header' ? mkEl() : null; },
    getElementById() { return null; },
    addEventListener(ev, fn) {
      (this._h[ev] = this._h[ev] || []).push(fn); // 收集全部 listener，避免互相覆盖
    },
  };
  return doc;
}

// ===== 1. index.html 不得无条件加载 desktop-app.js =====
assert.ok(desktopJs.length > 50, 'desktop-app.js 应非空');
assert.ok(
  !/<script[^>]+src=["'][^"']*desktop-app\.js["']/i.test(indexHtml),
  'index.html 不应有无条件加载 desktop-app.js 的 <script> 标签（web 端不得加载 app 专属脚本）'
);
assert.ok(
  indexHtml.includes('js/desktop-app.js'),
  'index.html 加载器应条件引用 js/desktop-app.js'
);

// ===== 2. app.js 暴露 window.VDL 共享 helper =====
// 注：app.js 里 Windows.VDL 是**多次** Object.assign 追加（管理面板等先追加过几处），
// 取最后一处才是「共享 helper」那段；旧的 split[1] 早已指向管理面板的增量块（断言误报）。
const _vdlParts = appJs.split('window.VDL = Object.assign');
const exposureBlock = (_vdlParts[_vdlParts.length - 1] || '').split('})();')[0];
assert.ok(exposureBlock.length > 0, 'app.js 应把共享 helper 暴露到 window.VDL');
for (const key of ['el', 'request', 'showError', 'switchView', 'createTaskCard']) {
  assert.ok(new RegExp('\\b' + key + '\\b').test(exposureBlock), `window.VDL 应暴露 ${key}`);
}

// ===== 3a. 桌面模式(pywebview.api 立即就绪)：退出按钮应显示并绑定 =====
{
  const els = { quitAppBtn: mkEl(), hideToDesktopBtn: mkEl() };
  const win = {
    VDL: { el: els },
    pywebview: { api: { quit_app() {}, hide_to_desktop() {} } },
    confirm: () => true,
  };
  const doc = mkDoc();
  runDesktop(win, doc);
  assert.equal(els.quitAppBtn.hidden, false, '桌面模式：退出按钮应显示');
  assert.ok(els.quitAppBtn._h.click, '桌面模式：退出按钮应绑定 click');
  // 2026-10-31：「返回桌面」按钮已下线（最小化交给系统标题栏），前端不得再唤醒它
  assert.equal(els.hideToDesktopBtn.hidden, true, '桌面模式：返回桌面按钮不应再显示');
  assert.ok(!els.hideToDesktopBtn._h.click, '桌面模式：返回桌面按钮不应再绑定 click');
}

// ===== 3b. web 模式(无 pywebview)：按钮保持隐藏，仅注册 pywebviewready 监听 =====
{
  const els = { quitAppBtn: mkEl(), hideToDesktopBtn: mkEl() };
  const doc = mkDoc();
  const win = { VDL: { el: els }, pywebview: undefined, confirm: () => true };
  runDesktop(win, doc);
  assert.equal(els.quitAppBtn.hidden, true, 'web 模式：退出按钮应保持隐藏');
  assert.equal(els.hideToDesktopBtn.hidden, true, 'web 模式：返回桌面按钮应保持隐藏');
  const readyFns = doc._h.pywebviewready || [];
  assert.ok(readyFns.length > 0, 'web 模式：应注册 pywebviewready 监听以等待桌面环境就绪');
  // 模拟 pywebview 稍后就绪：所有已注册 listener 都应执行
  win.pywebview = { api: { quit_app() {}, hide_to_desktop() {} } };
  readyFns.forEach((fn) => fn());
  assert.equal(els.quitAppBtn.hidden, false, 'pywebview 就绪后：退出按钮应显示');
  assert.equal(els.hideToDesktopBtn.hidden, true, 'pywebview 就绪后：返回桌面按钮仍保持隐藏');
}

// ===== 3c. window.VDL 缺失：应安全早退，不抛错、不触碰按钮 =====
{
  const els = { quitAppBtn: mkEl(), hideToDesktopBtn: mkEl() };
  const win = { VDL: undefined, pywebview: { api: {} } };
  const doc = mkDoc();
  assert.doesNotThrow(() => runDesktop(win, doc), 'window.VDL 缺失时不应抛错');
  assert.equal(els.quitAppBtn.hidden, true, 'VDL 缺失时按钮应保持隐藏');
}

console.log('test_frontend_isolation: ALL PASSED');
process.exit(0); // initSyncButton 的 setInterval 会阻止 Node 自然退出
