/* SelfMail 液态玻璃 Webmail · 前端逻辑（无框架，无外部依赖） */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  user: null, admin: false, csrf: "", folder: "INBOX",
  messages: [], current: null, timer: null,
};

/* ---------- API ---------- */
async function api(path, opt = {}) {
  const o = { method: opt.method || "GET", headers: {} };
  if (o.method !== "GET") { o.headers["X-CSRF"] = S.csrf; }
  if (opt.json !== undefined) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(opt.json);
  } else if (opt.form) {
    o.body = opt.form;
  }
  const r = await fetch(path, o);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("on"), 2600);
}

function show(id) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("on"));
  $(id).classList.add("on");
}

/* ---------- 主题 ---------- */
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem("sm-theme", mode);
}
applyTheme(localStorage.getItem("sm-theme") || "auto");
$("#theme-btn")?.addEventListener("click", () => {
  const cur = localStorage.getItem("sm-theme") || "auto";
  applyTheme(cur === "auto" ? "light" : cur === "light" ? "dark" : "auto");
  toast("外观：" + (localStorage.getItem("sm-theme") === "auto" ? "跟随系统" : localStorage.getItem("sm-theme") === "light" ? "浅色" : "深色"));
});

/* ---------- 3D 视差 ---------- */
function parallax() {
  const aura = $("#aura"), stage = $("#login-stage");
  window.addEventListener("pointermove", (e) => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const x = e.clientX / innerWidth - .5, y = e.clientY / innerHeight - .5;
    if (aura) aura.style.transform = `translate(${x * -26}px, ${y * -18}px)`;
    if (stage && $("#view-login").classList.contains("on"))
      stage.style.transform = `rotateY(${x * 7}deg) rotateX(${y * -6}deg)`;
  }, { passive: true });
}
parallax();

/* ---------- 登录 ---------- */
async function tryLogin() {
  const user = $("#li-user").value.trim(), pass = $("#li-pass").value;
  $("#li-err").textContent = "";
  try {
    const d = await api("/api/login", { method: "POST", json: { user, pass } });
    S.user = d.user; S.admin = d.admin; S.csrf = d.csrf;
    enterMail();
  } catch (e) { $("#li-err").textContent = e.message; }
}
$("#li-btn").addEventListener("click", tryLogin);
$("#li-pass").addEventListener("keydown", e => { if (e.key === "Enter") tryLogin(); });
$("#li-user").addEventListener("keydown", e => { if (e.key === "Enter") $("#li-pass").focus(); });

async function boot() {
  try {
    const me = await api("/api/me");
    S.user = me.user; S.admin = me.admin; S.csrf = me.csrf;
    $("#login-sub").textContent = `自托管 · 保密邮件系统 · @${me.domain}`;
    enterMail();
  } catch { show("#view-login"); }
}

function enterMail() {
  show("#view-mail");
  $("#admin-btn").style.display = S.admin ? "" : "none";
  loadFolders();
  loadMessages();
  clearInterval(S.timer);
  S.timer = setInterval(() => { loadFolders(); if (S.folder === "INBOX") loadMessages(true); }, 30000);
}

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  clearInterval(S.timer);
  S.user = null; S.csrf = "";
  show("#view-login");
});

/* ---------- 文件夹 ---------- */
async function loadFolders() {
  try {
    const list = await api("/api/folders");
    const names = { INBOX: "✉", Sent: "➤", Drafts: "✎", Trash: "🗑", Archive: "🗂" };
    $("#folders").innerHTML = list.map(f => `
      <div class="folder ${f.name === S.folder ? "on" : ""}" data-f="${esc(f.name)}">
        <span class="ico">${names[f.name] || "🗎"}</span>
        <span>${esc(f.name)}</span>
        <span class="cnt ${f.unseen ? "unseen" : ""}">${f.unseen ? f.unseen : f.messages ?? ""}</span>
      </div>`).join("");
    document.querySelectorAll(".folder").forEach(el =>
      el.addEventListener("click", () => selectFolder(el.dataset.f)));
  } catch (e) { if (e.message === "401") location.reload(); }
}
function selectFolder(f) {
  S.folder = f;
  $("#list-title").textContent = f;
  $("#mail-grid").classList.remove("with-read");
  closeRead();
  loadFolders(); loadMessages();
}
$("#folder-new").addEventListener("click", async () => {
  const name = prompt("新文件夹名称：");
  if (!name) return;
  await api("/api/folders", { method: "POST", json: { name } });
  loadFolders();
});

/* ---------- 邮件列表 ---------- */
async function loadMessages(silent) {
  try {
    const q = new URLSearchParams({ folder: S.folder, unread: $("#unseen-only").checked ? "1" : "0" });
    const kw = $("#search").value.trim();
    if (kw) q.set("q", kw);
    S.messages = await api("/api/messages?" + q);
    renderList();
  } catch (e) { if (!silent) toast("加载失败：" + e.message); }
}
function fmtDate(d) {
  if (!d) return "";
  const dt = new Date(d);
  if (isNaN(dt)) return d.slice(0, 16);
  const today = new Date().toDateString() === dt.toDateString();
  return today ? dt.toTimeString().slice(0, 5) : `${dt.getMonth() + 1}/${dt.getDate()}`;
}
function renderList() {
  const box = $("#mail-list");
  if (!S.messages.length) {
    box.innerHTML = `<div class="empty">此文件夹暂无邮件</div>`;
    return;
  }
  box.innerHTML = S.messages.map(m => {
    const unread = !m.flags.includes("\\Seen");
    const starred = m.flags.includes("\\Flagged");
    return `
    <div class="mail-item ${S.current === m.id ? "on" : ""}" data-id="${m.id}">
      ${unread ? `<span class="dot"></span>` : ""}
      <div style="flex:1;min-width:0">
        <div class="row1">
          <span class="from ${unread ? "unread" : ""}">${esc(shortFrom(m.from))}</span>
          ${m.attach ? `<span class="tag">附件</span>` : ""}
          ${starred ? `<span class="star">★</span>` : ""}
          <span class="date">${esc(fmtDate(m.date))}</span>
        </div>
        <div class="subj ${unread ? "unread" : ""}">${esc(m.subject || "(无主题)")}</div>
        <div class="prev">${esc((m.subject || "") && m.to ? "→ " + shortFrom(m.to) : "")}</div>
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll(".mail-item").forEach(el =>
    el.addEventListener("click", () => openMail(+el.dataset.id)));
}
function shortFrom(s) {
  const m = String(s || "").match(/"?([^"<]*)"?\s*<([^>]+)>/);
  return (m && m[1].trim()) || s || "";
}
$("#unseen-only").addEventListener("change", () => loadMessages());
let searchH;
$("#search").addEventListener("input", () => {
  clearTimeout(searchH);
  searchH = setTimeout(() => loadMessages(), 350);
});

/* ---------- 阅读 ---------- */
async function openMail(id) {
  S.current = id;
  const m = await api("/api/message/" + id);
  $("#mail-grid").classList.add("with-read");
  $("#read-empty").style.display = "none";
  $("#read").style.display = "";
  $("#r-subj").textContent = m.subject || "(无主题)";
  const h = m.headers || {};
  $("#r-meta").innerHTML =
    `<b>${esc(shortFrom(h.From || ""))}</b> &lt;${esc(h.From || "")}&gt;
     → ${esc(h.To || "")} · ${esc(fmtDate(h.Date || m.date))}`;
  $("#r-atts").innerHTML = (m.attachments || []).map(a =>
    `<a class="att" href="/api/attachment/${id}/${encodeURIComponent(a.part)}" download="${esc(a.name)}">
       ⬇ ${esc(a.name)} <span style="color:var(--ink3)">${(a.size / 1024).toFixed(0)}KB</span></a>`).join("");
  const body = $("#r-body");
  if (m.html) {
    const ih = m.html.replace(/<html[^>]*>|<\/html>|<head[\s\S]*?<\/head>|<body[^>]*>|<\/body>/gi, "");
    body.innerHTML = `<iframe sandbox="" id="r-frame" style="height:520px"></iframe>`;
    const f = $("#r-frame");
    f.srcdoc = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      body{font-family:-apple-system,'PingFang SC',sans-serif;padding:4px;color:#111;background:#fff}
      img{max-width:100%}</style></head><body>${ih}</body></html>`;
  } else {
    body.innerHTML = `<pre style="white-space:pre-wrap;font:inherit">${esc(m.text || "(空邮件)")}</pre>`;
  }
  renderList(); loadFolders();
  S._mailMeta = { subj: m.subject, from: h.From || "", msgid: h["Message-ID"] || "" };
}
function closeRead() {
  S.current = null;
  $("#read").style.display = "none";
  $("#read-empty").style.display = "";
}
$("#back-btn").addEventListener("click", () => {
  $("#mail-grid").classList.remove("with-read"); closeRead();
});

$("#r-del").addEventListener("click", async () => {
  await api("/api/delete", { method: "POST", json: { id: S.current } });
  toast("已删除"); closeRead(); loadMessages(); loadFolders();
});
$("#r-unread").addEventListener("click", async () => {
  await api("/api/flags", { method: "POST", json: { id: S.current, flag: "\\Seen", on: false } });
  loadMessages(); loadFolders(); toast("已标记未读");
});
$("#r-star").addEventListener("click", async () => {
  const cur = S.messages.find(m => m.id === S.current);
  const on = !(cur?.flags || "").includes("\\Flagged");
  await api("/api/flags", { method: "POST", json: { id: S.current, flag: "\\Flagged", on } });
  loadMessages(); toast(on ? "已标旗" : "已取消标旗");
});
$("#r-reply").addEventListener("click", () => {
  const meta = S._mailMeta || {};
  openCompose({ to: shortFrom(meta.from), subject: "回复：" + (meta.subj || ""), inreplyto: meta.msgid });
});

/* ---------- 撰写 ---------- */
function openCompose(pre = {}) {
  $("#c-to").value = pre.to || "";
  $("#c-cc").value = ""; $("#c-bcc").value = "";
  $("#c-subj").value = pre.subject || "";
  $("#c-body").value = "";
  $("#c-files").value = "";
  $("#c-atts").innerHTML = "";
  $("#c-send").dataset.inreplyto = pre.inreplyto || "";
  $("#compose-ov").classList.add("on");
}
$("#c-close").addEventListener("click", () => $("#compose-ov").classList.remove("on"));
$("#compose-ov").addEventListener("click", e => {
  if (e.target === e.currentTarget) e.currentTarget.classList.remove("on");
});

async function sendMail(draft) {
  const fd = new FormData();
  fd.append("to", $("#c-to").value);
  fd.append("cc", $("#c-cc").value);
  fd.append("bcc", $("#c-bcc").value);
  fd.append("subject", $("#c-subj").value);
  fd.append("body", $("#c-body").value);
  if ($("#c-send").dataset.inreplyto) fd.append("inreplyto", $("#c-send").dataset.inreplyto);
  for (const f of $("#c-files").files) fd.append("attachments", f);
  const st = $("#c-status");
  st.textContent = draft ? "保存中…" : "发送中…";
  try {
    const r = await api(draft ? "/api/draft" : "/api/send", { method: "POST", form: fd });
    st.textContent = "";
    $("#compose-ov").classList.remove("on");
    toast(draft ? "草稿已保存" : `已发送（本地 ${r.local} / 出站 ${r.queued}）`);
    selectFolder(draft ? "Drafts" : S.folder);
  } catch (e) { st.textContent = ""; toast("失败：" + e.message); }
}
$("#c-send").addEventListener("click", () => sendMail(false));
$("#c-draft").addEventListener("click", () => sendMail(true));

/* ---------- 改密 ---------- */
$("#settings-btn").addEventListener("click", () => $("#pw-ov").classList.add("on"));
$("#pw-cancel").addEventListener("click", () => $("#pw-ov").classList.remove("on"));
$("#pw-ok").addEventListener("click", async () => {
  try {
    await api("/api/password", { method: "POST", json: { old: $("#pw-old").value, new: $("#pw-new").value } });
    $("#pw-ov").classList.remove("on");
    $("#pw-old").value = $("#pw-new").value = "";
    toast("密码已修改");
  } catch (e) { toast(e.message); }
});

/* ---------- 管理后台 ---------- */
$("#admin-btn").addEventListener("click", () => { show("#view-admin"); loadAdmin("overview"); });
$("#admin-back").addEventListener("click", () => { show("#view-mail"); loadFolders(); });
$("#mails-btn").addEventListener("click", () => { show("#view-mail"); loadFolders(); });

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  document.querySelectorAll(".tab-panel").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  $("#tp-" + t.dataset.t).classList.add("on");
  loadAdmin(t.dataset.t);
}));

async function loadAdmin(which) {
  try {
    if (which === "overview") {
      const s = await api("/api/admin/status");
      const up = Math.floor(s.uptime / 3600) + "h" + Math.floor(s.uptime % 3600 / 60) + "m";
      const p = s.ports;
      $("#ov-body").innerHTML = `
        <div class="stat-row">
          <div class="stat"><span>版本</span><b>${esc(s.version)}</b></div>
          <div class="stat"><span>域名</span><b style="font-size:15px">${esc(s.domain)}</b></div>
          <div class="stat"><span>用户数</span><b>${s.users}</b></div>
          <div class="stat"><span>运行时间</span><b style="font-size:15px">${up}</b></div>
          <div class="stat"><span>出站队列</span><b>${s.queue}</b></div>
          <div class="stat"><span>数据体积</span><b style="font-size:15px">${(s.data_size / 1048576).toFixed(1)}MB</b></div>
          <div class="stat"><span>出站模式</span><b style="font-size:15px">${esc(s.outbound_mode)}</b></div>
          <div class="stat"><span>局域网IP</span><b style="font-size:15px">${esc(s.lan_ip)}</b></div>
        </div>
        <hr><h3>监听端口</h3>
        <div class="stat-row">
          ${Object.entries(p).map(([k, v]) => `<div class="stat"><span>${k}</span><b>${v || "—"}</b></div>`).join("")}
        </div>
        <hr><h3>DKIM 选择器：${esc(s.dkim_selector)} · 证书：${s.cert_exists ? "已就绪" : "未生成"}</h3>`;
    }
    if (which === "users") renderUsers();
    if (which === "aliases") renderAliases();
    if (which === "queue") renderQueue();
    if (which === "logs") renderLogs();
    if (which === "dns") renderDns();
  } catch (e) { toast(e.message); }
}

async function renderUsers() {
  const users = await api("/api/admin/users");
  $("#users-body").innerHTML = `<table><tr><th>账号</th><th>姓名</th><th>角色</th><th>配额MB</th><th>已用MB</th><th>状态</th><th>操作</th></tr>` +
    users.map(u => `<tr>
      <td><b>${esc(u.username)}</b></td><td>${esc(u.display_name || "")}</td>
      <td>${u.is_admin ? '<span class="badge ok">管理员</span>' : "普通"}</td>
      <td><input style="width:90px" type="number" value="${u.quota_mb}" data-q="${u.username}"></td>
      <td>${(u.used / 1048576).toFixed(1)}</td>
      <td>${u.active ? "正常" : '<span class="badge">停用</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn" data-act="q" data-u="${esc(u.username)}">存配额</button>
        <button class="btn" data-act="pw" data-u="${esc(u.username)}">重置密码</button>
        <button class="btn" data-act="tg" data-u="${esc(u.username)}">${u.active ? "停用" : "启用"}</button>
        <button class="btn danger" data-act="del" data-u="${esc(u.username)}">删除</button>
      </td></tr>`).join("") + `</table>`;
  $("#users-body").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
    const u = b.dataset.u;
    try {
      if (b.dataset.act === "q") {
        const v = +document.querySelector(`input[data-q="${u}"]`).value;
        await api(`/api/admin/users/${u}`, { method: "PATCH", json: { quota_mb: v } });
        toast("配额已更新");
      } else if (b.dataset.act === "pw") {
        const p = prompt(`为 ${u} 设置新密码（至少8位）：`);
        if (p) { await api(`/api/admin/users/${u}`, { method: "PATCH", json: { pass: p } }); toast("已重置"); }
      } else if (b.dataset.act === "tg") {
        await api(`/api/admin/users/${u}`, { method: "PATCH", json: { active: b.textContent === "停用" ? 0 : 1 } });
        renderUsers();
      } else if (b.dataset.act === "del") {
        if (confirm(`确定删除 ${u}？其全部邮件将被移除！`)) {
          await api(`/api/admin/users/${u}`, { method: "DELETE" });
          renderUsers(); toast("已删除");
        }
      }
    } catch (e) { toast(e.message); }
  }));
}
$("#nu-add").addEventListener("click", async () => {
  try {
    await api("/api/admin/users", { method: "POST", json: {
      user: $("#nu-user").value, display: $("#nu-name").value,
      pass: $("#nu-pass").value, quota_mb: +$("#nu-quota").value || 2048,
      admin: $("#nu-admin").value === "1" } });
    toast("已创建"); ["#nu-user", "#nu-name", "#nu-pass"].forEach(s => $(s).value = "");
    renderUsers();
  } catch (e) { toast(e.message); }
});

async function renderAliases() {
  const list = await api("/api/admin/aliases");
  $("#alias-body").innerHTML = `<table><tr><th>别名</th><th>目标</th><th></th></tr>` +
    list.map(a => `<tr><td>${esc(a.alias)}</td><td>${esc(a.target)}</td>
      <td><button class="btn danger" data-a="${esc(a.alias)}">删除</button></td></tr>`).join("") + `</table>`;
  $("#alias-body").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
    await api("/api/admin/aliases", { method: "POST", json: { delete: b.dataset.a } });
    renderAliases();
  }));
}
$("#na-add").addEventListener("click", async () => {
  await api("/api/admin/aliases", { method: "POST", json: { alias: $("#na-alias").value, target: $("#na-target").value } });
  $("#na-alias").value = $("#na-target").value = "";
  renderAliases();
});

async function renderQueue() {
  const q = await api("/api/admin/queue");
  $("#queue-body").innerHTML = q.length ? `<table><tr><th>#</th><th>发件人</th><th>收件人</th><th>状态</th><th>尝试</th><th>错误</th><th></th></tr>` +
    q.map(j => `<tr><td>${j.id}</td><td>${esc(j.sender)}</td><td>${esc(j.rcpt)}</td>
      <td>${j.status === "bounced" ? '<span class="badge">已退回</span>' : j.status}</td>
      <td>${j.attempts}</td><td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(j.last_error)}</td>
      <td><button class="btn" data-r="${j.id}">重试</button> <button class="btn danger" data-c="${j.id}">删除</button></td></tr>`).join("") + `</table>`
    : `<div class="empty">队列为空，出站畅通</div>`;
  $("#queue-body").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
    if (b.dataset.r) await api("/api/admin/queue", { method: "POST", json: { retry: b.dataset.r } });
    else await api("/api/admin/queue", { method: "POST", json: { clear: b.dataset.c } });
    renderQueue();
  }));
}
$("#q-retry-all").addEventListener("click", async () => {
  await api("/api/admin/queue", { method: "POST", json: { retry: "all" } }); renderQueue(); toast("已全部重试");
});
$("#q-clear-all").addEventListener("click", async () => {
  if (!confirm("清空整个出站队列？")) return;
  await api("/api/admin/queue", { method: "POST", json: { clear: "all" } }); renderQueue();
});

async function renderLogs() {
  const logs = await api("/api/admin/logs?n=300");
  const colors = { info: "var(--ink2)", warn: "var(--ink)", error: "var(--danger)" };
  $("#logs-body").innerHTML = `<table><tr><th>时间</th><th>级别</th><th>事件</th><th>详情</th><th>IP</th></tr>` +
    logs.map(l => `<tr><td style="white-space:nowrap">${esc(new Date(l.ts * 1000).toLocaleString())}</td>
      <td style="color:${colors[l.level] || "inherit"}">${l.level}</td><td>${esc(l.event)}</td>
      <td>${esc(l.detail)}</td><td>${esc(l.ip)}</td></tr>`).join("") + `</table>`;
}

async function renderDns() {
  const d = await api("/api/admin/dns");
  $("#dns-body").innerHTML = d.records.map(r =>
    `<div class="dns-rec"><b>[${r.type}]</b> ${esc(r.host)}<br>${esc(r.value)}<br><span style="color:var(--ink3)"># ${esc(r.note)}</span></div>`).join("");
}
$("#dns-check").addEventListener("click", async () => {
  $("#dns-check-body").innerHTML = "检测中…";
  try {
    const c = await api("/api/admin/dns-check");
    $("#dns-check-body").innerHTML = `
      <div class="dns-rec">MX   : ${c.mx.length ? esc(JSON.stringify(c.mx)) : "✗ 未找到"}</div>
      <div class="dns-rec">SPF  : ${esc(c.spf || "✗ 未找到")}</div>
      <div class="dns-rec">DKIM : ${esc(c.dkim || "✗ 未找到")}</div>
      <div class="dns-rec">DMARC: ${esc(c.dmarc || "✗ 未找到")}</div>
      <div class="dns-rec">A    : ${esc(JSON.stringify(c.hostname_a))}</div>`;
  } catch (e) { $("#dns-check-body").textContent = e.message; }
});

/* 启动 */
boot();
