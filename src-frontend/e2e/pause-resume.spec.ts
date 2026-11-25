import { test, expect } from "@playwright/test";

// 暂停/恢复必须在**运行中的任务**上验证，而 CI 的 e2e job 只起前端 dev server
// （无后端、无模型 Key）——此前的写法在 CI 里必然超时失败，却一直挂在流水线上。
// 现改为显式开启：
//   PLAYWRIGHT_LIVE=1 npx playwright test e2e/pause-resume.spec.ts
// 纯组件级行为已由 src/components/chat/__tests__/MessageItem.test.tsx 覆盖。
test.skip(!process.env.PLAYWRIGHT_LIVE, "需要真实后端 + 已配置模型 Key：设 PLAYWRIGHT_LIVE=1 后本机运行");

test("暂停与继续按钮切换状态", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("yuncat-onboarding-complete", "true"));
  await page.goto("/");

  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15000 });
  await textarea.fill("hello");
  await page.getByTestId("chat-send").click();

  // 任务卡片上的暂停菜单（data-testid 与文案解耦）
  await expect(page.getByTestId("task-pause")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("task-pause").click();
  await page.getByTestId("task-pause-now").click();

  // 暂停后出现「继续」按钮
  await expect(page.getByTestId("task-resume")).toBeVisible({ timeout: 5000 });
});
