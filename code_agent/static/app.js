"use strict";
let csrfToken = "";
let session = null;
let latestAnswer = "";
const element = id => document.getElementById(id);
const labels = {input:"接收请求",model:"规划 / 汇总",plan:"行动计划",tool_call:"调用工具",observation:"获取结果",output:"输出报告",limit:"达到上限"};

// Construct DOM nodes with textContent. Neither model output nor source becomes HTML.
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

async function configure() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error("无法连接本机服务。");
    const config = await response.json();
    csrfToken = config.csrf_token;
    element("mode").textContent = config.mode;
    element("exec-note").textContent = config.allow_exec ? "已开启可信测试执行。" : "当前仅读取与静态检查。";
  } catch (error) {
    element("status").textContent = error.message;
    element("run").disabled = true;
  }
}

element("review-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!csrfToken) return;
  const button = element("run");
  button.disabled = true;
  element("reset").disabled = true;
  element("status").classList.remove("error");
  element("status").textContent = "正在读取代码、调用工具并整理报告…";
  try {
    const response = await fetch("/api/review", {method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":csrfToken},
      body:JSON.stringify({task:element("task").value,target:element("target").value,session})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "审查失败。");
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
    element("status").textContent = result.status === "completed" ? "审查完成。可以继续追问，或切换目标对比修复结果。" : "审查达到预算上限，请缩小范围。";
    element("results").scrollIntoView({behavior:"smooth",block:"start"});
  } catch (error) {
    element("status").textContent = error.message;
    element("status").classList.add("error");
  } finally {
    button.disabled = false;
    element("reset").disabled = false;
  }
});
element("reset").addEventListener("click", () => {
  session = null;
  latestAnswer = "";
  element("results").hidden = true;
  element("status").classList.remove("error");
  element("status").textContent = "已新建会话，下一次审查从空白上下文开始。";
});
document.querySelectorAll("[data-target]").forEach(button => button.addEventListener("click", () => {element("target").value = button.dataset.target;}));
element("download").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([latestAnswer],{type:"text/markdown;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url; link.download = "code-review-report.md"; link.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
});
configure();
