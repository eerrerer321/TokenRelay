// 站長後台。跟使用者網頁一樣的安全規則：伺服器資料一律當純文字放進頁面（textContent），不用 innerHTML。
"use strict";

function $(id) {
  return document.getElementById(id);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

/** 會改東西的請求都帶 X-Admin-Request：別的網站沒辦法讓瀏覽器跨站送出這種請求（防 CSRF）。 */
async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Admin-Request": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new Error("連不到伺服器");
  }
  const data = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `請求失敗（HTTP ${resp.status}）`);
  }
  return data;
}

function showAlert(message, type = "danger") {
  const box = el("div", { class: `alert alert-${type} alert-dismissible fade show`, role: "alert" }, message,
    el("button", { type: "button", class: "btn-close", "data-bs-dismiss": "alert", "aria-label": "關閉" }));
  $("alerts").append(box);
  if (type === "success") setTimeout(() => bootstrap.Alert.getOrCreateInstance(box).close(), 4000);
}

async function guarded(fn) {
  try {
    await fn();
  } catch (err) {
    showAlert(err.message || String(err));
  }
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

function emptyRow(tbody, cols, text) {
  tbody.replaceChildren(el("tr", {}, el("td", { colspan: cols, class: "text-center text-body-secondary" }, text)));
}

/** "1.500000" → "1.5"；null → "" */
function trimPrice(value) {
  if (value === null || value === undefined || value === "") return "";
  return String(value).includes(".") ? String(value).replace(/0+$/, "").replace(/\.$/, "") : String(value);
}

/** 美元文字 → micro 整數；格式不對回傳 null */
function toMicro(text) {
  const match = String(text).trim().match(/^(\d+)(?:\.(\d{0,6}))?$/);
  if (!match) return null;
  return Number(match[1]) * 1e6 + Number((match[2] || "").padEnd(6, "0"));
}

function fromMicro(micro) {
  const whole = Math.floor(micro / 1e6);
  const frac = String(micro % 1e6).padStart(6, "0");
  return trimPrice(`${whole}.${frac}`);
}

// ---------------------------------------------------------------- 總覽

async function loadOverview() {
  const data = await api("/admin/api/overview");
  $("stat-users").textContent = data.users;
  $("stat-owed").textContent = trimPrice(data.owed_usd) || "0";
  $("stat-revenue").textContent = trimPrice(data.revenue_30d_usd) || "0";
  $("stat-requests").textContent = data.requests_30d;
  $("wallet-chain").textContent = data.chain;
  $("wallet-address").textContent = data.receive_address || "（config.ini 還沒填收款地址）";
  $("wallet-error").classList.toggle("d-none", !data.wallet_error);
  $("wallet-error").textContent = data.wallet_error ? "查不到餘額：" + data.wallet_error : "";
  const tbody = $("wallet-list");
  if (!data.wallet) return emptyRow(tbody, 2, data.wallet_error ? "—" : "儲值功能沒有開啟");
  tbody.replaceChildren(...Object.entries(data.wallet).map(([symbol, amount]) => el("tr", {},
    el("td", {}, symbol), el("td", { class: "text-end font-monospace" }, amount))));
}

$("overview-refresh").addEventListener("click", () => guarded(loadOverview));

// ---------------------------------------------------------------- 模型

let modelData = { models: [], upstreams: [], editable: false };
let editing = null; // 正在編輯的模型（null = 新增）

function ratio(sale, cost) {
  const s = toMicro(trimPrice(sale) || "x");
  const c = toMicro(trimPrice(cost) || "x");
  if (s === null || c === null || c === 0) return "";
  return "×" + (s / c).toFixed(2);
}

async function loadModels() {
  modelData = await api("/admin/api/models");
  $("models-readonly").classList.toggle("d-none", modelData.editable);
  $("model-add").disabled = !modelData.editable;
  const tbody = $("model-list");
  if (!modelData.models.length) return emptyRow(tbody, 11, "還沒有任何模型，按「新增模型」");
  const price = (v) => el("td", { class: "text-end font-monospace" }, trimPrice(v) || "—");
  tbody.replaceChildren(...modelData.models.map((m) => el("tr", { class: m.enabled ? "" : "text-body-secondary" },
    el("td", {}, el("div", { class: "form-check form-switch m-0" }, el("input", {
      class: "form-check-input", type: "checkbox", role: "switch", "aria-label": "開放 " + m.name,
      ...(m.enabled ? { checked: "" } : {}), ...(modelData.editable ? {} : { disabled: "" }),
      onchange: (event) => toggleModel(m, event.target),
    }))),
    el("td", {}, el("code", {}, m.name),
      m.kind === "chat" ? "" : el("span", { class: "badge text-bg-info ms-1" }, KIND_LABELS[m.kind] || m.kind)),
    el("td", { class: "small" }, `${m.upstream} / ${m.upstream_model}`),
    m.kind === "image" ? price(m.image_price && `${trimPrice(m.image_price)} / 張`) : price(m.input_price),
    price(m.kind === "chat" ? m.cached_input_price ?? m.input_price : null), price(m.output_price),
    price(m.cost_input_price), price(m.cost_cached_input_price), price(m.cost_output_price),
    el("td", { class: "text-end small" }, [ratio(m.input_price, m.cost_input_price),
      ratio(m.output_price, m.cost_output_price)].filter(Boolean).join(" / ") || "—"),
    el("td", { class: "text-end" }, el("button", {
      class: "btn btn-sm btn-outline-primary", type: "button", ...(modelData.editable ? {} : { disabled: "" }),
      onclick: () => openModelForm(m),
    }, "編輯")),
  )));
}

function toggleModel(model, input) {
  guarded(async () => {
    try {
      await api(`/admin/api/models/${encodeURIComponent(model.name)}`,
        { method: "PUT", body: { values: { enabled: input.checked ? "true" : "false" } } });
      showAlert(`${model.name} 已${input.checked ? "開放" : "下架"}`, "success");
    } finally {
      await loadModels();
    }
  });
}

// 表單欄位 → config.ini 的名稱
const FIELDS = ["kind", "upstream", "upstream_model", "input_price", "cached_input_price", "output_price",
  "image_price", "cost_input_price", "cost_cached_input_price", "cost_output_price", "max_output_tokens",
  "max_tokens_field", "image_tokens"];
const PRICE_FIELDS = new Set(FIELDS.filter((f) => f.endsWith("_price")));
const DEFAULTS = { kind: "chat", max_output_tokens: "4096", max_tokens_field: "max_tokens", image_tokens: "0" };
const KIND_LABELS = { chat: "對話", embedding: "embeddings", image: "產生圖片", speech: "文字轉語音" };
const KIND_HINTS = {
  chat: "使用者用 /v1/chat/completions。上游設了 responses = true 或 anthropic_base_url，也能用 Codex CLI、Claude Code。",
  embedding: "使用者用 /v1/embeddings，只收輸入 token。",
  image: "使用者用 /v1/images/generations，一次一張，按張數收費。",
  speech: "使用者用 /v1/audio/speech，按輸入字元數收費（不是 token）。",
};
const REQUIRED_BY_KIND = { chat: ["input_price", "output_price"], embedding: ["input_price"],
  speech: ["input_price"], image: ["image_price"] };

// 這個類型用不到的欄位：隱藏，儲存時清掉（寫回預設值）
function fieldUsed(form, field) {
  const box = form[field].closest("[data-kinds]");
  return !box || box.dataset.kinds.split(" ").includes(form.kind.value);
}

function applyKind() {
  const form = $("model-form");
  const kind = form.kind.value;
  for (const box of form.querySelectorAll("[data-kinds]")) {
    box.classList.toggle("d-none", !box.dataset.kinds.split(" ").includes(kind));
  }
  for (const field of FIELDS.filter((f) => PRICE_FIELDS.has(f))) {
    form[field].required = REQUIRED_BY_KIND[kind].includes(field);
  }
  $("f-in-label").textContent = kind === "speech" ? "每 100 萬字元" : "輸入";
  $("f-kind-hint").textContent = KIND_HINTS[kind];
}

$("f-kind").addEventListener("change", applyKind);

function openModelForm(model) {
  editing = model;
  const form = $("model-form");
  form.reset();
  $("upstream-model-options").replaceChildren();
  $("f-fetch-hint").textContent = "";
  $("f-upstream").replaceChildren(...modelData.upstreams.map((u) => el("option", { value: u }, u)));
  $("model-modal-title").textContent = model ? `編輯模型：${model.name}` : "新增模型";
  form.name.readOnly = Boolean(model);
  form.name.value = model ? model.name : "";
  form.enabled.checked = model ? model.enabled : true;
  for (const field of FIELDS) {
    const value = model ? model[field] : DEFAULTS[field];
    form[field].value = PRICE_FIELDS.has(field) ? trimPrice(value) : (value ?? "");
  }
  if (model && model.upstream_model === model.name) form.upstream_model.value = "";
  applyKind();
  $("model-delete").classList.toggle("d-none", !model);
  bootstrap.Modal.getOrCreateInstance($("model-modal")).show();
}

$("model-add").addEventListener("click", () => openModelForm(null));

$("f-apply-multiplier").addEventListener("click", () => {
  const form = $("model-form");
  const k = Number($("f-multiplier").value);
  if (!(k > 0)) return showAlert("倍數要是大於 0 的數字");
  const pairs = [["cost_input_price", "input_price"], ["cost_cached_input_price", "cached_input_price"],
    ["cost_output_price", "output_price"]];
  for (const [cost, sale] of pairs) {
    const micro = toMicro(form[cost].value);
    if (micro !== null && form[cost].value.trim() !== "") form[sale].value = fromMicro(Math.ceil(micro * k - 1e-9));
  }
});

let upstreamCatalog = [];

$("f-fetch").addEventListener("click", () => guarded(async () => {
  const name = $("f-upstream").value;
  $("f-fetch-hint").textContent = "載入中…";
  try {
    upstreamCatalog = await api(`/admin/api/upstreams/${encodeURIComponent(name)}/models`);
  } catch (err) {
    $("f-fetch-hint").textContent = "";
    throw err;
  }
  $("upstream-model-options").replaceChildren(...upstreamCatalog.map((m) => el("option", { value: m.id })));
  const priced = upstreamCatalog.filter((m) => m.cost_input_price).length;
  $("f-fetch-hint").textContent = `上游有 ${upstreamCatalog.length} 個模型，點欄位就能挑選`
    + (priced ? `；其中 ${priced} 個有附價格，選了會自動填進價` : "（這個上游沒附價格，進價要自己查）");
}));

$("f-upstream-model").addEventListener("change", (event) => {
  const form = $("model-form");
  const found = upstreamCatalog.find((m) => m.id === event.target.value);
  if (!found) return;
  for (const field of ["cost_input_price", "cost_cached_input_price", "cost_output_price"]) {
    if (found[field]) form[field].value = trimPrice(found[field]);
  }
  if (!editing && !form.name.value) form.name.value = found.id;
});

$("model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const values = {};
  for (const field of FIELDS) {
    const used = fieldUsed(form, field);
    const value = used ? form[field].value.trim() : "";
    if (editing) {
      const before = PRICE_FIELDS.has(field) ? trimPrice(editing[field]) : String(editing[field] ?? "");
      const unchanged = value === before || (field === "upstream_model" && value === "" && before === editing.name)
        || (!used && before === DEFAULTS[field]); // 用不到、本來就是預設值：不用在 config.ini 多寫一行空的
      if (!unchanged) values[field] = value; // 只送有改的，config.ini 才不會多出一堆沒用的行
    } else if (value !== "" && value !== DEFAULTS[field]) {
      values[field] = value;
    }
  }
  if (!editing || form.enabled.checked !== editing.enabled) values.enabled = form.enabled.checked ? "true" : "false";
  if (!editing && values.enabled === "true") delete values.enabled; // 預設就是開放
  const name = editing ? editing.name : form.name.value.trim();
  guarded(async () => {
    await api(`/admin/api/models/${encodeURIComponent(name)}`, { method: editing ? "PUT" : "POST", body: { values } });
    bootstrap.Modal.getInstance($("model-modal")).hide();
    showAlert(`已儲存 ${name}（寫回 config.ini，舊版存成 config.ini.bak）`, "success");
    await loadModels();
  });
});

$("model-delete").addEventListener("click", () => {
  if (!editing || !confirm(`確定要刪除 ${editing.name}？會從 config.ini 拿掉整個區段（舊版存成 config.ini.bak）。\n只是想暫時下架的話，關掉「開放」就好。`)) return;
  const name = editing.name;
  guarded(async () => {
    await api(`/admin/api/models/${encodeURIComponent(name)}`, { method: "DELETE" });
    bootstrap.Modal.getInstance($("model-modal")).hide();
    showAlert(`已刪除 ${name}`, "success");
    await loadModels();
  });
});

// ---------------------------------------------------------------- 使用者

let creditUser = null;

async function loadUsers() {
  const users = await api("/admin/api/users");
  const tbody = $("user-list");
  if (!users.length) return emptyRow(tbody, 5, "還沒有使用者");
  tbody.replaceChildren(...users.map((u) => el("tr", {},
    el("td", {}, u.id),
    el("td", {}, u.email),
    el("td", { class: "text-end font-monospace" }, trimPrice(u.balance_usd) || "0"),
    el("td", { class: "small" }, fmtTime(u.created_at)),
    el("td", { class: "text-end text-nowrap" },
      el("button", { class: "btn btn-sm btn-outline-secondary me-1", type: "button", onclick: () => resetLink(u) },
        "重設密碼連結"),
      el("button", { class: "btn btn-sm btn-outline-primary", type: "button", onclick: () => openCredit(u) },
        "調整餘額")),
  )));
}

function resetLink(user) {
  guarded(async () => {
    const data = await api(`/admin/api/users/${user.id}/reset-link`, { method: "POST" });
    $("reset-email").textContent = user.email;
    $("reset-minutes").textContent = data.expires_minutes;
    $("reset-link").value = data.link;
    $("reset-hint").textContent = data.link.startsWith("/")
      ? "config.ini 的 [site] public_url 沒填，所以只有路徑：前面要自己接上網站網址。" : "";
    bootstrap.Modal.getOrCreateInstance($("reset-modal")).show();
  });
}

$("reset-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("reset-link").value);
    $("reset-copy").textContent = "已複製";
    setTimeout(() => { $("reset-copy").textContent = "複製"; }, 1500);
  } catch {
    $("reset-link").select();
  }
});

// 關掉視窗就把連結清掉，不要留在畫面上
$("reset-modal").addEventListener("hidden.bs.modal", () => { $("reset-link").value = ""; });

function openCredit(user) {
  creditUser = user;
  $("credit-email").textContent = user.email;
  $("credit-form").reset();
  bootstrap.Modal.getOrCreateInstance($("credit-modal")).show();
}

$("credit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const user = creditUser;
  if (!confirm(`確定要調整 ${user.email} 的餘額 ${form.usd.value} 美元？`)) return;
  guarded(async () => {
    const data = await api(`/admin/api/users/${user.id}/credit`,
      { method: "POST", body: { usd: form.usd.value.trim(), note: form.note.value.trim() } });
    bootstrap.Modal.getInstance($("credit-modal")).hide();
    showAlert(`${user.email} 的餘額現在是 ${trimPrice(data.balance_usd) || "0"} 美元`, "success");
    await loadUsers();
  });
});

// ---------------------------------------------------------------- 分頁

const LOADERS = { "#tab-overview": loadOverview, "#tab-models": loadModels, "#tab-users": loadUsers };
for (const button of document.querySelectorAll('[data-bs-toggle="tab"]')) {
  button.addEventListener("shown.bs.tab", () => guarded(LOADERS[button.dataset.bsTarget]));
}
document.documentElement.dataset.bsTheme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
guarded(loadOverview);
