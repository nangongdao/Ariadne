import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "@/App";
import { initTheme } from "@/lib/theme";
// 变量字体：Noto Sans SC 承担中文主力（字重齐全、标题可加粗醒目），
// Inter 排在后面承接拉丁字符与数字（等宽数字更整齐）
import "@fontsource-variable/noto-sans-sc";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "@/styles/index.css";

// 在 React 挂载前定主题，避免首帧闪一下再切过去
initTheme();

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
