"use strict";
/**
 * auth-recovery.js —— refresh token 失效后的重新兑换策略
 *
 * 为什么单独成文件：access token 与 refresh token 都由「本次启动随机生成的
 * 静态 TOKEN」派生（Python 侧 auth_service._derive_signing_key）。后端一重启，两者同时失效，
 * 而主进程手里的静态 token 不变——所以刷新失败后必须回到静态 token 重新兑换，
 * 否则渲染进程拿着过期 token 会持续 403（token 中间件对无效 token 返 403 而非 401）。
 *
 * 实测 2026-09-25 实例：/api/v1/agents 同一时期既有 200（约 100 次）又有 403（32 次），
 * 另两个接口同型。抽成独立模块后可脱离主进程单独验证，
 * 同时不让 main.js（已超尺寸基线）继续变长。
 *
 * 依赖由调用方注入，模块自身无状态。
 */

/**
 * @param {Error} error 引发恢复的原始错误（refresh 失败）。
 * @param {() => Promise<{ok: boolean, token?: string|null, error?: string}>} exchangeStatic
 *        用静态 token 重新兑换；成功时已把新 token 写入主进程状态。
 * @param {() => string|null} getAccessToken 读当前 access token。
 * @returns {Promise<{ok: boolean, token?: string|null, error?: string}>} 与 refresh 同形状的结果。
 */
async function recoverAuthAfterRefreshFailure(error, exchangeStatic, getAccessToken) {
  const rescued = await exchangeStatic();
  const fresh = getAccessToken();
  if (rescued && rescued.ok && fresh) {
    return { ok: true, token: fresh };
  }
  return { ok: false, error: (error && error.message) || 'refresh_failed' };
}

module.exports = { recoverAuthAfterRefreshFailure };
