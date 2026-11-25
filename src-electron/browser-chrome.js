// browser-chrome.js —— 内置浏览器工具栏的行为（标签条 / 地址栏 / 导航按钮）
//
// 工具栏只做「人怎么操作浏览器」这件事；AI 侧的操控走主进程的宿主桥，
// 两边最终都落到同一套窗口与标签上，所以人类随时能接手 AI 打开过的页面。

(function () {
  "use strict";

  var api = window.yuncatBrowser;
  var tabsEl = document.getElementById("tabs");
  var addressEl = document.getElementById("address");
  var agentEl = document.getElementById("agent");
  var statusEl = document.getElementById("status");
  var backEl = document.getElementById("back");
  var forwardEl = document.getElementById("forward");
  var reloadEl = document.getElementById("reload");
  var newTabEl = document.getElementById("newtab");

  var latest = { tabs: [], active_id: null, agent_active: false };
  var addressDirty = false;

  function flash(text) {
    statusEl.textContent = text;
    window.setTimeout(function () { if (statusEl.textContent === text) statusEl.textContent = ""; }, 4000);
  }

  function send(cmd, payload) {
    return api.command(cmd, payload || {}).catch(function (error) {
      flash(String(error && error.message ? error.message : error));
    });
  }

  function tabLabel(tab) {
    if (tab.loading) return "载入中…";
    if (tab.error) return tab.error;
    return tab.title || tab.url || "新标签页";
  }

  function renderTabs(state) {
    tabsEl.textContent = "";
    state.tabs.forEach(function (tab) {
      var el = document.createElement("div");
      el.className = "tab" + (tab.active ? " active" : "");
      el.title = tab.url || "";
      var label = document.createElement("span");
      label.className = "label";
      label.textContent = tabLabel(tab);
      var close = document.createElement("button");
      close.className = "close";
      close.textContent = "×";
      close.title = "关闭标签页";
      close.addEventListener("click", function (event) {
        event.stopPropagation();
        send("closeTab", { id: tab.id });
      });
      el.addEventListener("click", function () { send("activateTab", { id: tab.id }); });
      el.appendChild(label);
      el.appendChild(close);
      tabsEl.appendChild(el);
    });
  }

  function render(state) {
    latest = state || latest;
    renderTabs(latest);
    var active = (latest.tabs || []).filter(function (t) { return t.active; })[0] || null;
    if (!addressDirty) addressEl.value = active && active.url && active.url !== "about:blank" ? active.url : "";
    agentEl.hidden = !latest.agent_active;
    backEl.disabled = !active;
    forwardEl.disabled = !active;
    reloadEl.disabled = !active;
    document.title = active ? "Yuncat 浏览器 — " + tabLabel(active) : "Yuncat 浏览器";
  }

  addressEl.addEventListener("focus", function () { addressDirty = true; addressEl.select(); });
  addressEl.addEventListener("blur", function () { addressDirty = false; });
  addressEl.addEventListener("input", function () { addressDirty = true; });
  addressEl.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var url = addressEl.value.trim();
    if (!url) return;
    addressDirty = false;
    addressEl.blur();
    send("navigate", { url: url });
  });

  backEl.addEventListener("click", function () { send("back"); });
  forwardEl.addEventListener("click", function () { send("forward"); });
  reloadEl.addEventListener("click", function () { send("reload"); });
  newTabEl.addEventListener("click", function () { send("newTab", {}); });

  api.onState(render);
  api.state().then(render).catch(function () { /* 降级：首帧拿不到状态时等下一次推送 */ });
})();