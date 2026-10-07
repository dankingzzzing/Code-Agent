"use strict";
// Render with DOM nodes. Raw HTML stays text and links only permit HTTP(S).
function inlineMarkdown(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const value = match[0];let node;
    if (value.startsWith("**")) {node=document.createElement("strong");node.textContent=value.slice(2,-2);}
    else if (value.startsWith("`")) {node=document.createElement("code");node.textContent=value.slice(1,-1);}
    else {
      const link=/^\[([^\]]+)\]\(([^)]+)\)$/.exec(value);let url;
      try {url=new URL(link[2]);} catch {url=null;}
      if (url && ["http:","https:"].includes(url.protocol)) {node=document.createElement("a");node.href=url.href;node.target="_blank";node.rel="noopener noreferrer";}
      else node=document.createElement("span");
      node.textContent=link[1];
    }
    parent.append(node);offset=match.index+value.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function renderMarkdown(host, text) {
  host.replaceChildren();
  const lines=text.replace(/\r\n/g,"\n").split("\n");
  let code=null,list=null,paragraph=[];
  const flush=()=>{if(paragraph.length){const p=document.createElement("p");inlineMarkdown(p,paragraph.join("\n"));host.append(p);paragraph=[];}};
  const cells=line=>line.trim().replace(/^\||\|$/g,"").split(/(?<!\\)\|/).map(s=>s.trim().replace(/\\\|/g,"|"));
  for(let i=0;i<lines.length;i++) {
    const line=lines[i],trim=line.trim();
    if(/^\s*```/.test(line)){flush();list=null;if(code)code=null;else{const pre=document.createElement("pre");code=document.createElement("code");pre.append(code);host.append(pre);}continue;}
    if(code){code.textContent+=line+"\n";continue;}
    if(!trim){flush();list=null;continue;}
    if(line.includes("|") && i+1<lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])){
      flush();list=null;const wrapper=document.createElement("div");wrapper.className="table-scroll";
      const table=document.createElement("table"),head=document.createElement("thead"),row=document.createElement("tr");
      for(const cell of cells(line)){const th=document.createElement("th");inlineMarkdown(th,cell);row.append(th);}
      head.append(row);table.append(head);const body=document.createElement("tbody");i+=2;
      while(i<lines.length && lines[i].includes("|") && lines[i].trim()){
        const tr=document.createElement("tr");for(const cell of cells(lines[i])){const td=document.createElement("td");inlineMarkdown(td,cell);tr.append(td);}body.append(tr);i++;
      }
      i--;table.append(body);wrapper.append(table);host.append(wrapper);continue;
    }
    const heading=/^\s*(#{1,6})\s+(.+)$/.exec(line);
    if(heading){flush();list=null;const h=document.createElement("h"+heading[1].length);inlineMarkdown(h,heading[2]);host.append(h);continue;}
    if(/^\s*([-*_])\1\1+\s*$/.test(line)){flush();list=null;host.append(document.createElement("hr"));continue;}
    const item=/^\s*(?:[-*+] |\d+[.)] )(.+)$/.exec(line);
    if(item){flush();const tag=/^\s*\d/.test(line)?"ol":"ul";if(!list||list.tagName.toLowerCase()!==tag){list=document.createElement(tag);host.append(list);}const li=document.createElement("li");inlineMarkdown(li,item[1]);list.append(li);continue;}
    if(trim.startsWith("> ")){flush();list=null;const quote=document.createElement("blockquote");inlineMarkdown(quote,trim.slice(2));host.append(quote);continue;}
    list=null;paragraph.push(line);
  }
  flush();
}
