"use strict";
let csrfToken = "", profile = null, session = null, latestAnswer = "";
let selection = null, activeSettings = null;
const element = id => document.getElementById(id);
const labels = {scan:"扫描目录",input:"接收请求",model:"规划 / 汇总",plan:"行动计划",tool_call:"调用工具",observation:"获取结果",output:"输出报告",limit:"达到上限"};

function renderReport(text) { renderMarkdown(element("report"), text); }

let reportDraft = "", paintTimer = null;
function appendEvent(event) {
  const item=document.createElement("li");
  if(event.ok===false)item.classList.add("failed");
  const title=document.createElement("strong");
  title.textContent=`${String(event.step).padStart(2,"0")} ${labels[event.kind]||event.kind}${event.tool?" / "+event.tool:""}`;
  const detail=document.createElement("span");
  detail.textContent=event.summary+(event.arguments?" · "+JSON.stringify(event.arguments):"");
  item.append(title,detail);element("trace").append(item);
  element("trace").scrollTop=element("trace").scrollHeight;
  element("live-stage").textContent=event.summary;
  if(typeof event.progress==="number")element("review-progress").value=event.progress;
  if(event.kind==="model") {reportDraft="";element("report").textContent="模型正在检查证据并整理报告…";}
}
function appendText(value) {
  reportDraft+=value;
  if(paintTimer===null)paintTimer=setTimeout(()=>{renderReport(reportDraft);paintTimer=null;},100);
}
async function streamReview(payload) {
  const response=await fetch("/api/review/stream",{method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":csrfToken},body:JSON.stringify(payload)});
  if(!response.ok){const value=await response.json();const error=new Error(value.error||"审查请求失败");error.needsSettings=value.needs_settings;throw error;}
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",result=null;
  function dispatch(line){
    if(!line.trim())return;
    const frame=JSON.parse(line);
    if(frame.type==="event")appendEvent(frame.data);
    else if(frame.type==="text")appendText(frame.data);
    else if(frame.type==="result")result=frame.data;
    else if(frame.type==="error"){const error=new Error(frame.data.error||"审查失败");error.needsSettings=frame.data.needs_settings;throw error;}
  }
  try {
    while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});let index;
      while((index=buffer.indexOf("\n"))>=0){dispatch(buffer.slice(0,index));buffer=buffer.slice(index+1);}
      if(done)break;
    }
    if(buffer.trim())dispatch(buffer);
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
  if(!result)throw new Error("审查连接提前结束，尚未收到完整报告。");
  return result;
}

async function request(path, payload) {
  const response = await fetch(path, {method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":csrfToken},body:JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) {const error=new Error(result.error || "请求失败，请检查本机服务。");error.needsSettings=result.needs_settings;throw error;}
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
  for (const id of ["apply-settings","use-demo","close-settings","api-key","model-name","base-url","api-style","remember-settings"]) element(id).disabled = busy;
}

function openSettings() {
  element("close-settings").hidden = !profile;
  element("api-key").value = "";
  element("api-key").required = !activeSettings?.api_key_configured;
  element("api-key").placeholder = activeSettings?.api_key_configured ? "密钥已保存，留空继续使用" : "填写你的 API Key";
  if (!element("model-dialog").open) element("model-dialog").showModal();
}

async function configure() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error("无法连接本机服务。");
    activeSettings = await response.json();
    csrfToken = activeSettings.csrf_token;
    element("mode").textContent = activeSettings.mode;
    element("exec-note").textContent = activeSettings.allow_exec ? "已开启可信测试执行。" : "当前仅读取与静态检查。";
    element("model-name").value = activeSettings.model;
    element("base-url").value = activeSettings.base_url;
    element("api-style").value = activeSettings.api_style || "auto";
    element("remember-settings").checked = activeSettings.remembered !== false || !activeSettings.configured;
    element("settings-status").textContent = activeSettings.bootstrap_error || "连接测试会验证模型的工具调用与结果回传。";
    if (activeSettings.configured && activeSettings.profile) {profile=activeSettings.profile;workspaceBusy(false);status("已恢复上次模型配置，API Key 无需重新输入。");}
    else openSettings();
  } catch (error) { status(error.message, true); }
}

async function saveSettings(demo) {
  if (!csrfToken) return;
  settingsBusy(true);
  const message = element("settings-status");
  message.classList.remove("error");
  message.textContent = demo ? "正在进入离线演示…" : "正在验证模型连接和工具调用，请稍候…";
  try {
    const payload = {profile, demo, verify:!demo, remember:element("remember-settings").checked};
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
    if(error.needsSettings){element("api-key").required=true;element("api-key").placeholder="保存的密钥鉴权失败，请填写新的有效密钥";}
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
  if(!profile){openSettings();return;}
  workspaceBusy(true);status("正在准备项目审查…");
  const started=Date.now();let clock=null;
  try {
    if(!selection||selection.path!==element("target").value.trim())useSelection(await request("/api/select",{profile,path:element("target").value.trim()}));
    element("results").hidden=false;element("live-status").hidden=false;element("coverage-summary").hidden=true;
    element("trace").replaceChildren();element("report").textContent="正在扫描并读取源码…";
    element("metrics").textContent="审查进行中，执行进度和报告将实时显示。";
    element("review-progress").value=0;reportDraft="";latestAnswer="";
    element("download").disabled=true;element("live-elapsed").textContent="0 秒";
    clock=setInterval(()=>{element("live-elapsed").textContent=Math.floor((Date.now()-started)/1000)+" 秒";},1000);
    element("results").scrollIntoView({behavior:"smooth",block:"start"});
    const result=await streamReview({task:element("task").value,profile,selection:selection.selection,target:selection.target,session});
    session=result.session;latestAnswer=result.answer;
    element("download").disabled=false;
    if(paintTimer!==null){clearTimeout(paintTimer);paintTimer=null;}
    renderReport(result.answer);
    element("metrics").textContent=`${result.provider} · ${result.steps} 个步骤 · ${result.tool_calls} 次工具调用`;
    if(result.coverage){const c=result.coverage;element("coverage-summary").hidden=false;element("coverage-summary").textContent=`发现 ${c.discovered} 个代码文件，完整读取 ${c.read} 个，逐文件语义审查 ${c.semantic_reviewed} 个，跳过 ${c.skipped.length} 个。`;}
    element("review-progress").value=1;
    status(result.status==="completed"?"审查完成，报告包含覆盖统计、逐文件证据和实际测试状态。":"审查未完整完成，请查看覆盖限制。");
  } catch(error){
    status(error.message,true);element("live-stage").textContent="审查失败："+error.message;
    if(error.needsSettings){openSettings();element("api-key").required=true;element("api-key").placeholder="请填写新的有效密钥";element("settings-status").textContent=error.message;element("settings-status").classList.add("error");}
  } finally {if(clock)clearInterval(clock);if(paintTimer!==null){clearTimeout(paintTimer);paintTimer=null;}workspaceBusy(false);}
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
