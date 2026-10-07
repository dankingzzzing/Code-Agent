"use strict";
let csrfToken = "", profile = null, session = null, latestAnswer = "";
let selection = null, activeSettings = null;
const element = id => document.getElementById(id);
const labels = {input:"接收请求",model:"规划 / 汇总",plan:"行动计划",tool_call:"调用工具",observation:"获取结果",output:"输出报告",limit:"达到上限"};

function renderReport(text) {
  const host = element("report");
  host.replaceChildren();
  let code = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      if (code) { code = null; } else { code = document.createElement("pre"); host.append(code); }
      continue;
    }
    if (code) { code.textContent += line + "\n"; continue; }
    if (!line.trim()) continue;
    let tag = "p", value = line;
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) { tag = "h" + heading[1].length; value = heading[2]; }
    else if (line.startsWith("> ")) { tag = "blockquote"; value = line.slice(2); }
    const node = document.createElement(tag);
    node.textContent = value;
    host.append(node);
  }
}

async function request(path, payload) {
  const response = await fetch(path, {method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":csrfToken},body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败，请检查本机服务。");
  return result;
}

function status(message, error = false) {
  element("status").textContent = message;
  element("status").classList.toggle("error", error);
}

function clearConversation() {
  session = null;
  latestAnswer = "";
  element("results").hidden = true;
}

function workspaceBusy(busy) {
  for (const id of ["run","reset","model-settings","pick-file","pick-directory","target","task"]) {
    element(id).disabled = busy || (!profile && id === "run");
  }
  document.querySelectorAll("[data-target]").forEach(button => {button.disabled = busy;});
}

function settingsBusy(busy) {
  for (const id of ["apply-settings","use-demo","close-settings","api-key","model-name","base-url","api-style"]) element(id).disabled = busy;
}

function openSettings() {
  element("close-settings").hidden = !profile;
  element("api-key").value = "";
  element("api-key").required = !activeSettings?.api_key_configured;
  element("api-key").placeholder = activeSettings?.api_key_configured ? "已在本机配置，留空沿用已有密钥" : "填写你的 API Key";
  if (!element("model-dialog").open) element("model-dialog").showModal();
}

async function configure() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error("无法连接本机服务。");
    activeSettings = await response.json();
    csrfToken = activeSettings.csrf_token;
    element("mode").textContent = "请先配置模型";
    element("exec-note").textContent = activeSettings.allow_exec ? "已开启可信测试执行。" : "当前仅读取与静态检查。";
    element("model-name").value = activeSettings.model;
    element("base-url").value = activeSettings.base_url;
    element("api-style").value = "auto";
    element("settings-status").textContent = activeSettings.bootstrap_error || "连接测试会验证模型的工具调用与结果回传。";
    openSettings();
  } catch (error) { status(error.message, true); }
}

async function saveSettings(demo) {
  if (!csrfToken) return;
  settingsBusy(true);
  const message = element("settings-status");
  message.classList.remove("error");
  message.textContent = demo ? "正在进入离线演示…" : "正在验证模型连接和工具调用，请稍候…";
  try {
    const payload = {profile, demo, verify:!demo};
    if (!demo) Object.assign(payload, {api_key:element("api-key").value.trim(),model:element("model-name").value.trim(),base_url:element("base-url").value.trim(),api_style:element("api-style").value});
    const result = await request("/api/settings", payload);
    profile = result.profile;
    activeSettings = {...activeSettings,...result};
    element("api-key").value = "";
    element("api-key").required = false;
    element("base-url").value = result.base_url;
    element("mode").textContent = result.mode;
    clearConversation();
    element("model-dialog").close();
    workspaceBusy(false);
    status(demo ? "已进入离线演示。选择文件或文件夹后开始审查。" : "模型连接与工具调用验证成功。选择本地代码后开始审查。");
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  } finally { settingsBusy(false); }
}

element("model-form").addEventListener("submit", event => {event.preventDefault();saveSettings(false);});
element("use-demo").addEventListener("click", () => saveSettings(true));
element("model-settings").addEventListener("click", openSettings);
element("close-settings").addEventListener("click", () => element("model-dialog").close());
element("model-dialog").addEventListener("cancel", event => {if (!profile) event.preventDefault();});

function useSelection(value) {
  selection = value;
  element("target").value = value.path;
  element("selection-note").textContent = `当前审查范围：${value.workspace}`;
  clearConversation();
  status("已选择本地代码，可以开始审查。");
}

async function pick(kind) {
  if (!profile) {openSettings();return;}
  workspaceBusy(true);
  status(kind === "file" ? "请在电脑弹出的窗口中选择代码文件…" : "请在电脑弹出的窗口中选择代码文件夹…");
  try {
    const result = await request("/api/pick", {profile,kind});
    if (result.cancelled) status("已取消选择，保留原来的审查范围。");
    else useSelection(result);
  } catch (error) {status(error.message, true);}
  finally {workspaceBusy(false);}
}
element("pick-file").addEventListener("click", () => pick("file"));
element("pick-directory").addEventListener("click", () => pick("directory"));

element("review-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!profile) {openSettings();return;}
  workspaceBusy(true);
  status("正在读取代码、调用工具并整理报告…");
  try {
    if (!selection || selection.path !== element("target").value.trim()) {
      const result = await request("/api/select", {profile,path:element("target").value.trim()});
      useSelection(result);
      status("正在读取代码、调用工具并整理报告…");
    }
    const result = await request("/api/review", {task:element("task").value,profile,selection:selection.selection,target:selection.target,session});
    session = result.session;
    latestAnswer = result.answer;
    renderReport(result.answer);
    element("metrics").textContent = `${result.provider} · ${result.steps} 个步骤 · ${result.tool_calls} 次工具调用`;
    element("trace").replaceChildren();
    for (const event of result.events) {
      const item = document.createElement("li");
      if (event.ok === false) item.classList.add("failed");
      const title = document.createElement("strong");
      title.textContent = `${String(event.step).padStart(2,"0")} ${labels[event.kind] || event.kind}${event.tool ? " / " + event.tool : ""}`;
      const detail = document.createElement("span");
      detail.textContent = event.arguments ? JSON.stringify(event.arguments) : event.summary;
      item.append(title,detail);
      element("trace").append(item);
    }
    element("results").hidden = false;
    status(result.status === "completed" ? "审查完成。可以继续追问，或切换目标对比修复结果。" : "审查达到预算上限，请缩小范围。");
    element("results").scrollIntoView({behavior:"smooth",block:"start"});
  } catch (error) {status(error.message, true);}
  finally {workspaceBusy(false);}
});

element("reset").addEventListener("click", () => {clearConversation();status("已新建会话，保留当前模型和审查目录。");});
document.querySelectorAll("[data-target]").forEach(button => button.addEventListener("click", () => {
  element("target").value = button.dataset.target;
  selection = null;
  clearConversation();
  element("selection-note").textContent = "示例路径相对于启动目录。";
}));
element("download").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([latestAnswer],{type:"text/markdown;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url; link.download = "code-review-report.md"; link.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
});
configure();
