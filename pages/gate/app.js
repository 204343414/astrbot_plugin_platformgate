/* 平台能力门禁 WebUI */
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
let state = null;
let currentPlatform = "aiocqhttp";

function setNotice(text, error = false) {
  const el = $("notice");
  el.textContent = text || "";
  el.className = "notice " + (error ? "error" : text ? "ok" : "hidden");
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function effectiveAllowed(plugin) {
  const val = state.rules[currentPlatform]?.[plugin.name];
  if (val !== undefined) return val;
  return state.default_allow_all;
}

function countAllowed() {
  let c = 0;
  for (const p of state.plugins || []) if (effectiveAllowed(p)) c += 1;
  return c;
}

function renderStats() {
  $("stat-plugins").textContent = (state.plugins || []).length;
  const pc = [...(state.plugins || [])].filter(
    (p) => state.rules.aiocqhttp?.[p.name]
  ).length;
  const qc = [...(state.plugins || [])].filter(
    (p) => state.rules.qq_official?.[p.name]
  ).length;
  $("stat-aiocqhttp").textContent = pc;
  $("stat-qq").textContent = qc;
}

function renderList() {
  const list = $("plugin-list");
  const plugins = (state.plugins || []).filter((p) => !p.reserved);
  $("empty").classList.toggle("hidden", plugins.length > 0);
  list.innerHTML = "";
  for (const p of plugins) {
    const allowed = effectiveAllowed(p);
    const card = document.createElement("div");
    card.className = "plugin";
    card.innerHTML = `
      <div class="plugin-head">
        <label class="check">
          <input type="checkbox" data-name="${esc(p.name)}" ${allowed ? "checked" : ""} />
        </label>
        <div class="plugin-title">
          <span class="name">${esc(p.display_name || p.name)}</span>
          <span class="badge">v${esc(p.version || "")}</span>
          ${p.reserved ? '<span class="reserved">保留插件</span>' : ""}
          <div class="meta">${esc(p.name)}${p.author ? " · " + esc(p.author) : ""}${p.activated ? "" : " · ⚠️ 未激活"}</div>
          ${p.desc ? `<div class="desc">${esc(p.desc)}</div>` : ""}
        </div>
      </div>
      <div class="plugin-tags">
        ${p.commands && p.commands.length ? `<span class="tag-label">指令:</span>` + p.commands.map((c) => `<span class="tag cmd">/${esc(c)}</span>`).join("") : ""}
        ${p.tools && p.tools.length ? `<span class="tag-label">工具:</span>` + p.tools.map((t) => `<span class="tag tool">${esc(t)}</span>`).join("") : ""}
        ${!p.commands && !p.tools ? '<span class="tag">（无指令/工具）</span>' : ""}
      </div>`;
    list.appendChild(card);
  }
}

function refreshStats() {
  renderStats();
  renderList();
  renderLlmNote();
}

function renderLlmNote() {
  const note = $("llm-block-note");
  if (!note) return;
  const blocked = (state.llm_block_platforms || []).map((p) => {
    return p === "qq_official" ? "官方" : p === "aiocqhttp" ? "napcat" : p;
  }).join("、");
  note.innerHTML = blocked
    ? `<div class="llm-block-title">🚫 已拦截 LLM 说话平台</div><div class="llm-block-body">${esc(blocked)}</div><div class="llm-block-hint">在 AstrBot 插件配置里改 block_llm_speech_platforms（逗号分隔平台名）可调整。</div>`
    : `<div class="llm-block-title">💬 LLM 说话：所有平台放行</div><div class="llm-block-hint">配置 block_llm_speech_platforms 可指定拦截平台（如 qq_official）。</div>`;
}

async function save(platform, plugin, allow) {
  try {
    const res = await bridge.apiPost("set", { platform, plugin, allow });
    state.rules[platform] = state.rules[platform] || {};
    if (allow === null) delete state.rules[platform][plugin];
    else state.rules[platform][plugin] = allow;
    setNotice(`已保存：${plugin} 在 ${platform} ${allow ? "放行" : "拦截"}`, false);
  } catch (e) {
    setNotice("保存失败：" + (e && e.message ? e.message : e), true);
  }
  refreshStats();
}

async function load() {
  await bridge.ready();
  state = await bridge.apiGet("bootstrap");
  bindEvents();
  refreshStats();
  setNotice("配置已加载。勾选/取消即可切换平台白名单。", false);
}

function bindEvents() {
  document.querySelectorAll(".platform").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".platform").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentPlatform = btn.dataset.platform;
      renderList();
    });
  });

  $("plugin-list").addEventListener("change", (e) => {
    if (e.target.matches("input[type=checkbox]")) {
      const plugin = e.target.dataset.name;
      const allow = e.target.checked;
      save(currentPlatform, plugin, allow);
    }
  });

  $("btn-refresh").addEventListener("click", async () => {
    setNotice("刷新中…", false);
    state = await bridge.apiGet("bootstrap");
    setNotice("注册表已刷新。", false);
    refreshStats();
  });
}

load();
