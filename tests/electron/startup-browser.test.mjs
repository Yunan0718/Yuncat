import { test } from "node:test";
import assert from "node:assert/strict";
import startup from "../../src-electron/startup-browser.js";

const { browserBridgeTimeoutMs, startOptionalBrowserIntegration } = startup;

test("browser bridge timeout is bounded and configurable", () => {
  const previous = process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS;
  try {
    process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS = "2500";
    assert.equal(browserBridgeTimeoutMs(), 2500);
    process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS = "10";
    assert.equal(browserBridgeTimeoutMs(), 5000);
  } finally {
    if (previous === undefined) delete process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS;
    else process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS = previous;
  }
});

test("optional browser bridge cannot block backend startup and late bridges close", async () => {
  const previous = process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS;
  const previousError = console.error;
  let closed = false;
  process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS = "500";
  console.error = () => {};
  try {
    const integration = await startOptionalBrowserIntegration(async () => {
      await new Promise((resolve) => setTimeout(resolve, 700));
      return { close: async () => { closed = true; } };
    }, {});
    assert.equal(integration, null);
    await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(closed, true);
  } finally {
    console.error = previousError;
    if (previous === undefined) delete process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS;
    else process.env.YUNCAT_BROWSER_BRIDGE_TIMEOUT_MS = previous;
  }
});
