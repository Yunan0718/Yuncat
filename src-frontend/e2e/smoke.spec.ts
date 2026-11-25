import { test, expect } from "@playwright/test";

test("首页加载并出现品牌或核心元素", async ({ page }) => {
  await page.goto("/");
  // 等待页面标题或正文出现 Yuncat / 发送 / New chat 中任意一个
  await expect(page).toHaveTitle(/Yuncat/, { timeout: 15000 });
  const body = page.locator("body");
  await expect(body).toContainText(/Yuncat|发送|New chat|chat\.send|chat\.newChat|startup/, { timeout: 15000 });
  await page.screenshot({ path: "e2e/screenshots/smoke.png", fullPage: true });
});
