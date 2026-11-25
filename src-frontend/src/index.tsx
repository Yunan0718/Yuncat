import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { App } from "./App";
import "./index.css";
import "./i18n";

// 隐藏启动 loading（index.html 里的 spinner），React 挂载后移除
const loader = document.getElementById("root-loading");
if (loader) loader.style.display = "none";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>,
);
