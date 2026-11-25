import { test, expect } from "@playwright/test";

// 2026-09-15 修复：原选择器 `[aria-label='chat.send']` / `button:has-text('chat.send')`
// 用的是 **i18n 键名**，而按钮实际 aria-label 是翻译后的文案（zh 下是「发送」）——
// 选择器永远匹配不到，这条用例在 CI 里不可能通过。改用 data-testid，与文案/语言解耦。
test("发送消息并断言消息出现在列表", async ({ page }) => {
  // 首次启动会弹 4 步引导向导并遮住输入区，先标记已完成，避免与发送流程互相干扰
  await page.addInitScript(() => localStorage.setItem("yuncat-onboarding-complete", "true"));
  await page.goto("/");

  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15000 });

  await textarea.fill("hello");
  await expect(page.getByTestId("chat-send")).toBeEnabled({ timeout: 5000 });
  await page.getByTestId("chat-send").click();

  // 乐观发送：消息文本应立即出现在消息列表
  await expect(page.locator(".message-list").first()).toContainText("hello", { timeout: 5000 });
});
