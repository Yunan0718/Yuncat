"use strict";
/**
 * browser-cdp.js —— 内置浏览器的 CDP 原语（页面快照 / 点击 / 输入 / 截图）
 *
 * 为什么单独成文件：browser.js 负责窗口与标签生命周期，这里只负责「怎么操作一个页面」。
 * 两者拆开后各自都能压在 800 行以内，也方便用假 webContents 单测（tests/electron）。
 *
 * 为什么用 CDP 而不是 `element.click()`：
 *   `element.click()` 绕过命中测试，遇到遮罩层、自定义下拉、canvas 应用时会给出
 *   「点了但没反应」的假成功。这里统一走 `webContents.debugger` 派发真实鼠标/键盘事件，
 *   与用户在窗口里亲手操作等价。
 *
 * 边界：本模块只操作传入的 webContents，不持有窗口引用，也不读写磁盘。
 */

/**
 * CDP 事件域 enable 的超时——只为订阅事件，不值得让每个动作都等满 15s。
 * 实测该命令可能完全不回应（见 attach 注释），因此必须短且失败可继续。
 */
const DOMAIN_ENABLE_TIMEOUT_MS = 3000;
/** 单条 CDP 命令的默认超时（页面卡死时不能把工具调用拖成「一直等」） */
const CDP_TIMEOUT_MS = 15000;
/** 导航后等待 readyState=complete 的默认超时；超时不算失败（长连接页面很常见） */
const NAVIGATE_TIMEOUT_MS = 20000;
/** 快照默认上限：元素数与正文长度都设上限，避免把整页 DOM 灌进模型上下文 */
const SNAPSHOT_DEFAULTS = { maxNodes: 120, includeText: true, maxTextChars: 6000, maxNameChars: 80 };
/** 截图上限（显式人为上限：整页截图高度/总像素封顶，防止一张图吃掉几百 MB 内存） */
const SCREENSHOT_MAX_HEIGHT = 4000;
const SCREENSHOT_MAX_PIXELS = 16000000;

/** 具名按键 -> Windows 虚拟键码（Input.dispatchKeyEvent 需要） */
const KEY_CODES = {
  Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, Space: 32,
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  Home: 36, End: 35, PageUp: 33, PageDown: 34, Insert: 45, F5: 116,
};
const MODIFIER_BITS = { alt: 1, ctrl: 2, control: 2, meta: 4, cmd: 4, shift: 8 };

/** 允许落地的协议白名单——file:/javascript:/data: 一律拒绝，避免把浏览器变成本地文件读取器 */
const ALLOWED_PROTOCOLS = new Set(["http:", "https:", "about:"]);

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clampInt(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.round(n), min), max);
}

/** 带超时的 CDP 命令；超时抛错而不是无限等，调用方能把失败原因讲清楚 */
function sendWithTimeout(wc, method, params, timeoutMs) {
  const dbg = wc.debugger;
  if (!dbg || typeof dbg.sendCommand !== "function") {
    return Promise.reject(new Error("CDP unavailable for this page"));
  }
  if (!dbg.isAttached()) return Promise.reject(new Error("CDP not attached"));
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`CDP ${method} timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  return Promise.race([dbg.sendCommand(method, params || {}), timeout])
    .finally(() => { if (timer) clearTimeout(timer); });
}

function send(wc, method, params, timeoutMs) {
  return sendWithTimeout(wc, method, params, timeoutMs || CDP_TIMEOUT_MS);
}

/**
 * 已成功开启过事件域的 webContents（WeakSet，随页面回收）——避免每次动作都重发 enable。
 * 这些 enable 只是「订阅事件」，不是命令执行的前置条件，因此失败也不该拦住动作。
 */
const domainsEnabled = new WeakSet();

/**
 * 附着 CDP；重复调用幂等。
 *
 * 2026-10-07 修复：原实现把 `await Page.enable` / `await Runtime.enable` 当成动作的前置门，
 * 任一 enable 不回应就整条链路卡死。实测（真实应用内调用宿主桥）：Page.enable 每次都
 * 在 15000ms 超时，于是 open / read / snapshot / click / screenshot **全部**失败，
 * 内置浏览器完全不可用。
 *
 * 关键事实：CDP 里 `Page.enable` / `Runtime.enable` 只负责**订阅事件**，
 * `Page.navigate` / `Runtime.evaluate` / `Input.*` / `Page.captureScreenshot` 这些命令
 * 并不依赖它们。所以这里改成：附着必须成功（失败即报错），enable 降级为 best-effort，
 * 用短超时 + 记日志，不再阻塞动作本身。
 */
async function attach(wc) {
  if (!wc || wc.isDestroyed()) throw new Error("页面已关闭");
  const dbg = wc.debugger;
  if (!dbg) throw new Error("CDP unavailable for this page");
  if (!dbg.isAttached()) dbg.attach("1.3");
  if (!domainsEnabled.has(wc)) {
    // 两个域**各自独立**尝试：实测这里 Page.enable 会一直不回应，
    // 若把它们串在一个 try 里，失败会连带跳过 Runtime.enable——多丢一层能力。
    let anyEnabled = false;
    for (const domain of ["Runtime.enable", "Page.enable"]) {
      try {
        await send(wc, domain, {}, DOMAIN_ENABLE_TIMEOUT_MS);
        anyEnabled = true;
      } catch (e) {
        // 降级（best-effort）：事件域没开只影响「推送事件」，不影响命令式动作。
        // 记日志留痕，避免把「失败」和「本来就没开」混为一谈。
        console.warn(`[browser] CDP ${domain} unavailable: ${e && e.message}`);
      }
    }
    // 全都失败也不缓存，下次动作再试（页面可能只是还没就绪）。
    if (anyEnabled) domainsEnabled.add(wc);
  }
  return true;
}

function executeJavaScript(wc, script) {
  if (!wc || wc.isDestroyed()) return Promise.reject(new Error("页面已关闭"));
  return wc.executeJavaScript(script, true);
}

/** 把「用户或模型给的地址」规范化：补协议、拒绝危险协议 */
function normalizeUrl(input) {
  const raw = String(input || "").trim();
  if (!raw) throw new Error("URL 为空");
  const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`;
  let parsed;
  try {
    parsed = new URL(withScheme);
  } catch (e) {
    throw new Error(`无法解析的地址：${raw}`);
  }
  if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) {
    throw new Error(`出于安全考虑，只允许 http/https 地址（收到 ${parsed.protocol}）`);
  }
  return parsed.toString();
}

async function waitForReady(wc, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!wc || wc.isDestroyed()) return false;
    try {
      const state = await send(wc, "Runtime.evaluate", {
        expression: "document.readyState", returnByValue: true,
      }, 3000);
      if (state && state.result && state.result.value === "complete") return true;
    } catch (e) {
      // 降级：页面正在切换文档时 evaluate 会短暂失败，继续轮询即可
    }
    await delay(150);
  }
  return false;
}

async function navigate(wc, url, timeoutMs) {
  const target = normalizeUrl(url);
  await attach(wc);
  const result = await send(wc, "Page.navigate", { url: target });
  if (result && result.errorText) throw new Error(`导航失败：${result.errorText}`);
  const loaded = await waitForReady(wc, timeoutMs || NAVIGATE_TIMEOUT_MS);
  return { url: target, loaded };
}

function historyApi(wc) {
  const nav = wc.navigationHistory;
  return nav && typeof nav.goBack === "function" ? nav : wc;
}

async function goBack(wc) {
  const nav = historyApi(wc);
  if (typeof nav.canGoBack === "function" && !nav.canGoBack()) return { moved: false, reason: "已经在最早一页" };
  nav.goBack();
  await waitForReady(wc, NAVIGATE_TIMEOUT_MS);
  return { moved: true };
}

async function goForward(wc) {
  const nav = historyApi(wc);
  if (typeof nav.canGoForward === "function" && !nav.canGoForward()) return { moved: false, reason: "已经在最新一页" };
  nav.goForward();
  await waitForReady(wc, NAVIGATE_TIMEOUT_MS);
  return { moved: true };
}

async function reload(wc) {
  wc.reload();
  await waitForReady(wc, NAVIGATE_TIMEOUT_MS);
  return { reloaded: true };
}

/**
 * 页面内快照函数：给可交互元素编号（data-yuncat-ref）并返回结构。
 *
 * 通过 `.toString()` 注入页面执行，因此这里必须自包含：不引用外层变量，
 * 只依赖浏览器 API。结构对齐 Playwright/MCP 的 ref 语义，模型可以「看编号再点编号」。
 */
function pageSnapshotSource(maxNodes, includeText, maxTextChars, maxNameChars) {
  var SELECTOR = 'a[href],button,input:not([type="hidden"]),select,textarea,summary,' +
    '[contenteditable="true"],[role="button"],[role="link"],[role="checkbox"],[role="radio"],' +
    '[role="tab"],[role="menuitem"],[role="option"],[role="combobox"],[role="textbox"],' +
    '[role="switch"],[role="searchbox"],[tabindex="0"]';

  function isVisible(el) {
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) === 0) return false;
    var rect = el.getBoundingClientRect();
    return rect.width >= 2 && rect.height >= 2;
  }

  function nameOf(el) {
    var raw = el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.getAttribute('alt') || '';
    if (!raw && el.tagName === 'INPUT') raw = el.value || '';
    if (!raw) raw = el.innerText || el.textContent || '';
    return String(raw).replace(/\s+/g, ' ').trim().slice(0, maxNameChars);
  }

  function roleOf(el) {
    var explicit = el.getAttribute('role');
    if (explicit) return explicit;
    var tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'disclosure';
    if (tag === 'input') {
      var type = (el.type || 'text').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset' || type === 'image') return 'button';
      if (type === 'checkbox' || type === 'radio') return type;
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  }

  var previous = document.querySelectorAll('[data-yuncat-ref]');
  for (var p = 0; p < previous.length; p++) previous[p].removeAttribute('data-yuncat-ref');

  var nodes = [];
  var candidates = document.querySelectorAll(SELECTOR);
  for (var i = 0; i < candidates.length && nodes.length < maxNodes; i++) {
    var el = candidates[i];
    if (!isVisible(el)) continue;
    var rect = el.getBoundingClientRect();
    var ref = nodes.length + 1;
    el.setAttribute('data-yuncat-ref', String(ref));
    nodes.push({
      ref: ref,
      role: roleOf(el),
      name: nameOf(el),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      disabled: !!el.disabled,
      checked: typeof el.checked === 'boolean' ? el.checked : null,
      in_viewport: rect.top < window.innerHeight && rect.bottom > 0 &&
        rect.left < window.innerWidth && rect.right > 0,
      box: { x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) },
    });
  }

  return {
    url: location.href,
    title: document.title,
    total_candidates: candidates.length,
    truncated: candidates.length > nodes.length,
    viewport: { width: window.innerWidth, height: window.innerHeight, scroll_y: Math.round(window.scrollY) },
    nodes: nodes,
    text: includeText ? String(document.body ? document.body.innerText : '').slice(0, maxTextChars) : '',
  };
}

async function buildSnapshot(wc, options) {
  const o = Object.assign({}, SNAPSHOT_DEFAULTS, options || {});
  const maxNodes = clampInt(o.maxNodes, 1, 600, SNAPSHOT_DEFAULTS.maxNodes);
  const maxTextChars = clampInt(o.maxTextChars, 0, 200000, SNAPSHOT_DEFAULTS.maxTextChars);
  const maxNameChars = clampInt(o.maxNameChars, 16, 400, SNAPSHOT_DEFAULTS.maxNameChars);
  await attach(wc);
  const script = `(${pageSnapshotSource.toString()})(${maxNodes}, ${o.includeText ? "true" : "false"}, ${maxTextChars}, ${maxNameChars})`;
  return executeJavaScript(wc, script);
}

function targetExpression(target) {
  if (target && target.selector) return `document.querySelector(${JSON.stringify(String(target.selector))})`;
  if (target && target.ref !== undefined && target.ref !== null && target.ref !== "") {
    return `document.querySelector('[data-yuncat-ref="' + ${JSON.stringify(String(target.ref))} + '"]')`;
  }
  return "null";
}

/** 定位目标：滚进视口（可选聚焦）后返回盒子，找不到返回 null */
async function inspectTarget(wc, target, focus) {
  const expr = targetExpression(target);
  if (expr === "null") throw new Error("必须提供 ref（来自 browser_snapshot）或 selector");
  const script = `(function(){var el = ${expr};
    if (!el) return null;
    if (el.scrollIntoView) el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'auto' });
    if (${focus ? "true" : "false"}) {
      if (typeof el.focus === 'function') el.focus();
      if (typeof el.select === 'function') {
        try { el.select(); } catch (e) { /* 降级：个别元素不支持 select，聚焦仍然生效 */ }
      }
    }
    var r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), type: el.type || '', x: r.left, y: r.top,
             w: r.width, h: r.height, value: typeof el.value === 'string' ? el.value.slice(0, 200) : '' };
  })()`;
  return executeJavaScript(wc, script);
}

const STALE_TARGET_HINT = "目标不在页面上（页面可能已重绘），请重新调用 browser_snapshot 取新的 ref";

async function clickTarget(wc, target, options) {
  const opts = options || {};
  const button = opts.button === "right" ? "right" : "left";
  await attach(wc);
  const box = await inspectTarget(wc, target, true);
  if (!box) throw new Error(STALE_TARGET_HINT);
  const x = Math.round(box.x + box.w / 2);
  const y = Math.round(box.y + box.h / 2);
  const buttons = button === "left" ? 1 : 2;
  await send(wc, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
  await send(wc, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, clickCount: 1, buttons });
  await send(wc, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, clickCount: 1, buttons: 0 });
  await delay(200);
  return { clicked: { tag: box.tag, type: box.type }, x, y };
}

async function typeText(wc, text, target, options) {
  const opts = options || {};
  await attach(wc);
  if (target && (target.ref !== undefined || target.selector)) {
    const box = await inspectTarget(wc, target, true);
    if (!box) throw new Error(STALE_TARGET_HINT);
  }
  const value = String(text == null ? "" : text);
  await send(wc, "Input.insertText", { text: value });
  if (opts.submit) await pressKey(wc, "Enter");
  return { typed_chars: value.length, submitted: !!opts.submit };
}

function keyEventParams(key) {
  const name = String(key || "").trim();
  if (!name) throw new Error("按键名称为空");
  const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
  const virtualKey = KEY_CODES[name] || KEY_CODES[capitalized] ||
    (name.length === 1 ? name.toUpperCase().charCodeAt(0) : 0);
  if (!virtualKey) throw new Error(`不认识的按键：${name}`);
  return { key: name, code: name, windowsVirtualKeyCode: virtualKey, nativeVirtualKeyCode: virtualKey };
}

function modifierMask(modifiers) {
  let mask = 0;
  for (const m of modifiers || []) {
    const bit = MODIFIER_BITS[String(m).toLowerCase()];
    if (bit) mask |= bit;
  }
  return mask;
}

async function pressKey(wc, key, modifiers) {
  await attach(wc);
  const params = keyEventParams(key);
  params.modifiers = modifierMask(modifiers);
  const isText = params.key.length === 1;
  const down = Object.assign({}, params, { type: isText ? "keyDown" : "rawKeyDown" });
  if (isText) down.text = params.key;
  await send(wc, "Input.dispatchKeyEvent", down);
  await send(wc, "Input.dispatchKeyEvent", Object.assign({}, params, { type: "keyUp" }));
  return { key: params.key };
}

async function scrollBy(wc, dx, dy) {
  await attach(wc);
  const viewport = await executeJavaScript(wc, "(function(){return {w: window.innerWidth, h: window.innerHeight};})()");
  const x = Math.round((viewport && viewport.w ? viewport.w : 800) / 2);
  const y = Math.round((viewport && viewport.h ? viewport.h : 600) / 2);
  await send(wc, "Input.dispatchMouseEvent", {
    type: "mouseWheel", x, y,
    deltaX: clampInt(dx, -10000, 10000, 0), deltaY: clampInt(dy, -20000, 20000, 0),
  });
  await delay(250);
  const after = await executeJavaScript(wc, "Math.round(window.scrollY)");
  return { scroll_y: typeof after === "number" ? after : null };
}

async function readPage(wc, maxChars) {
  const limit = clampInt(maxChars, 200, 400000, 12000);
  await attach(wc);
  const script = `(function(){return {url: location.href, title: document.title,
    text: String(document.body ? document.body.innerText : '').slice(0, ${limit})};})()`;
  return executeJavaScript(wc, script);
}

async function screenshot(wc, options) {
  const opts = options || {};
  await attach(wc);
  const params = { format: "png", fromSurface: true };
  if (opts.fullPage) {
    const metrics = await send(wc, "Page.getLayoutMetrics");
    const size = (metrics && (metrics.cssContentSize || metrics.contentSize)) || {};
    const width = clampInt(size.width, 1, 4000, 1280);
    const height = clampInt(size.height, 1, SCREENSHOT_MAX_HEIGHT, 800);
    const scale = Math.min(1, Math.sqrt(SCREENSHOT_MAX_PIXELS / Math.max(1, width * height)));
    params.captureBeyondViewport = true;
    params.clip = { x: 0, y: 0, width, height, scale: Number(scale.toFixed(3)) };
  }
  const result = await send(wc, "Page.captureScreenshot", params, 30000);
  if (!result || !result.data) throw new Error("截图失败：CDP 未返回图像数据");
  return Buffer.from(result.data, "base64");
}

async function evaluate(wc, expression) {
  await attach(wc);
  const result = await send(wc, "Runtime.evaluate", {
    expression: String(expression || ""), returnByValue: true, awaitPromise: true, userGesture: true,
  }, 30000);
  if (result && result.exceptionDetails) {
    const detail = result.exceptionDetails.exception || {};
    throw new Error(`脚本报错：${detail.description || result.exceptionDetails.text || "unknown"}`);
  }
  return result && result.result ? result.result.value : null;
}

async function waitFor(wc, options) {
  const opts = options || {};
  const timeoutMs = clampInt(opts.timeoutMs, 200, 60000, 8000);
  const deadline = Date.now() + timeoutMs;
  if (!opts.selector && !opts.text) throw new Error("wait_for 需要 selector 或 text");
  while (Date.now() < deadline) {
    let found = false;
    try {
      if (opts.selector) {
        found = !!(await executeJavaScript(wc, `!!document.querySelector(${JSON.stringify(String(opts.selector))})`));
      } else {
        found = !!(await executeJavaScript(
          wc, `document.body ? document.body.innerText.includes(${JSON.stringify(String(opts.text))}) : false`));
      }
    } catch (e) {
      // 降级：导航过程中页面上下文会被替换，evaluate 失败属预期，继续轮询
    }
    if (found) return { found: true };
    await delay(250);
  }
  return { found: false, timeout_ms: timeoutMs };
}

function detach(wc) {
  try { domainsEnabled.delete(wc); } catch (e) { /* 已回收：无需清理 */ }
  try {
    if (wc && !wc.isDestroyed() && wc.debugger && wc.debugger.isAttached()) wc.debugger.detach();
  } catch (e) {
    // 降级：窗口正在销毁时 detach 失败无副作用，忽略即可
  }
}

module.exports = {
  attach, detach, send, navigate, goBack, goForward, reload, normalizeUrl,
  buildSnapshot, clickTarget, typeText, pressKey, scrollBy, readPage, screenshot,
  evaluate, waitFor, executeJavaScript, clampInt,
  SNAPSHOT_DEFAULTS, CDP_TIMEOUT_MS, NAVIGATE_TIMEOUT_MS,
  SCREENSHOT_MAX_HEIGHT, SCREENSHOT_MAX_PIXELS, ALLOWED_PROTOCOLS,
};