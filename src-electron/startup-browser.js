"use strict";

/** 可选的内置浏览器桥不应无限期阻塞主后端启动。 */
function browserBridgeTimeoutMs() {
  const configured = Number(process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS);
  return Number.isFinite(configured) && configured >= 500 && configured <= 30000 ? configured : 5000;
}

async function startOptionalBrowserIntegration(startBrowserIntegration, deps) {
  const pending = startBrowserIntegration(deps);
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timed out after ${browserBridgeTimeoutMs()}ms`)), browserBridgeTimeoutMs());
    });
    const integration = await Promise.race([pending, timeout]);
    console.log('[startup] optional browser integration ready');
    return integration;
  } catch (error) {
    console.error('[browser] integration unavailable; continuing without browser bridge:', error.message);
    // 迟到的桥仍要关闭，避免后台端口泄漏。
    void pending.then((late) => late.close()).catch((lateError) => {
      console.error('[browser] late integration cleanup failed:', lateError.message);
    });
    return null;
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { browserBridgeTimeoutMs, startOptionalBrowserIntegration };
