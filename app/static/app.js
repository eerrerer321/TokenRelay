// AI 模型轉發站 — 使用者網頁
//
// 安全規則（朋友會來攻擊，請維持）：
// 1. 伺服器回來的資料一律用 textContent 放進頁面，不用 innerHTML。
//    不然有人把 <script> 塞進 key 名稱之類的欄位，就能在別人的瀏覽器裡執行程式（XSS）。
// 2. 不寫 inline script／onclick：伺服器的 CSP 標頭只允許執行 /app.js 和鎖定版本的 CDN 檔案。
"use strict";

const TOKEN_KEY = "relay.session";
const POLL_MS = 10000;

// ---------------------------------------------------------------- 小工具

function $(id) {
  return document.getElementById(id);
}

/** 建立元素。children 裡的字串會變成純文字節點（不會被當成 HTML）。 */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}

function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* 瀏覽器禁止儲存時，重新整理就要重新登入 */ }
}

/** API 回傳的時間是 UTC（結尾 Z），這裡轉成瀏覽器所在時區顯示。 */
function fmtTime(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("zh-TW", { hour12: false });
}

function errorText(data, status) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return "輸入格式錯誤：" + detail.map((d) => d.msg).join("；");
  if (data && data.error && data.error.message) return data.error.message;
  return `請求失敗（HTTP ${status}）`;
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError("連不到伺服器，請確認網路", 0);
  }
  const data = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (!resp.ok) {
    if (resp.status === 401 && token) {
      // 登入過期：清掉 token 回登入頁
      setToken(null);
      route();
    }
    throw new ApiError(errorText(data, resp.status), resp.status);
  }
  return data;
}

function showAlert(message, type = "danger") {
  const box = el("div", { class: `alert alert-${type} alert-dismissible fade show`, role: "alert" },
    message,
    el("button", { type: "button", class: "btn-close", "data-bs-dismiss": "alert", "aria-label": "關閉" }));
  $("alerts").append(box);
  if (type === "success") setTimeout(() => bootstrap.Alert.getOrCreateInstance(box).close(), 5000);
}

function clearAlerts() {
  $("alerts").replaceChildren();
}

/** 包住非同步動作：失敗時顯示錯誤訊息，而不是默默沒反應。 */
async function guarded(fn) {
  try {
    await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401 && !getToken()) return; // 已經回登入頁了
    showAlert(err.message || String(err));
  }
}

function badge(text, color) {
  return el("span", { class: `badge text-bg-${color}` }, text);
}

function emptyRow(tbody, cols, text) {
  tbody.replaceChildren(el("tr", {}, el("td", { colspan: cols, class: "text-center text-body-secondary" }, text)));
}

// 狀態碼 → 中文與顏色
const USAGE_STATUS = {
  ok: ["成功", "success"],
  pending: ["進行中", "info"],
  upstream_unreachable: ["上游連不到（不收費）", "secondary"],
  upstream_error: ["上游錯誤（不收費）", "secondary"],
  upstream_timeout: ["上游逾時（先收預扣，待站長確認）", "warning"],
  upstream_broken: ["上游中途斷線", "warning"],
  bad_upstream_response: ["上游回應異常", "warning"],
  no_usage: ["上游沒給用量", "warning"],
  interrupted: ["中途中斷", "warning"],
  abandoned: ["伺服器重啟時未完成", "warning"],
  error: ["錯誤", "danger"],
};
const DEPOSIT_STATUS = {
  pending: ["待付款", "warning"],
  paid: ["已入帳", "success"],
  expired: ["已過期", "secondary"],
};
const LEDGER_KIND = {
  hold: "預扣",
  settle: "結算（多退少補）",
  deposit: "儲值",
  deposit_manual: "人工入帳",
  adjust: "站長調整",
};

function statusBadge(map, status) {
  const [text, color] = map[status] || [status, "secondary"];
  return badge(text, color);
}

// ---------------------------------------------------------------- 複製按鈕

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-from]");
  if (!button) return;
  const source = $(button.dataset.copyFrom);
  const text = "value" in source && source.tagName === "INPUT" ? source.value : source.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    showAlert("瀏覽器不允許自動複製，請手動選取複製");
    return;
  }
  const original = button.textContent;
  button.textContent = "已複製";
  setTimeout(() => { button.textContent = original; }, 1500);
});

// ---------------------------------------------------------------- 登入／註冊

$("tab-login").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  guarded(async () => {
    const data = await api("/api/login", {
      method: "POST",
      body: { email: form.email.value, password: form.password.value },
    });
    setToken(data.token);
    form.reset();
    clearAlerts();
    if (!location.hash || location.hash === "#") location.hash = "#overview";
    route();
  });
});

$("tab-register").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  guarded(async () => {
    await api("/api/register", {
      method: "POST",
      body: { email: form.email.value, password: form.password.value, invite_code: form.invite_code.value },
    });
    $("login-email").value = form.email.value;
    form.reset();
    bootstrap.Tab.getOrCreateInstance(document.querySelector('[data-bs-target="#tab-login"]')).show();
    showAlert("註冊成功，請登入", "success");
  });
});

$("logout-btn").addEventListener("click", () => {
  guarded(async () => {
    try { await api("/api/logout", { method: "POST" }); } finally {
      setToken(null);
      location.hash = "";
      route();
    }
  });
});

// ---------------------------------------------------------------- 共用：帳號資訊

async function refreshMe() {
  const me = await api("/api/me");
  $("nav-email").textContent = me.email;
  $("nav-balance").textContent = "$" + me.balance_usd;
  $("nav-balance").className = "badge fs-6 " + (me.balance_micro > 0 ? "text-bg-success" : "text-bg-danger");
  $("overview-balance").textContent = me.balance_usd;
  return me;
}

// ---------------------------------------------------------------- 總覽

async function loadOverview() {
  $("api-base").textContent = location.origin + "/v1";
  const rows = await api("/api/usage?limit=5");
  const tbody = $("overview-usage");
  if (!rows.length) return emptyRow(tbody, 5, "還沒有任何請求");
  tbody.replaceChildren(...rows.map((u) => el("tr", {},
    el("td", {}, fmtTime(u.created_at)),
    el("td", {}, u.model),
    el("td", {}, statusBadge(USAGE_STATUS, u.status)),
    el("td", { class: "text-end" }, `${u.prompt_tokens ?? "-"} / ${u.completion_tokens ?? "-"}`),
    el("td", { class: "text-end font-monospace" }, u.cost_usd),
  )));
}

// ---------------------------------------------------------------- 模型與價格

const MODEL_KINDS = { chat: "對話", embedding: "embeddings", image: "產生圖片", speech: "文字轉語音" };

async function loadModels() {
  const rows = await api("/api/models");
  const tbody = $("model-list");
  if (!rows.length) return emptyRow(tbody, 7, "站長還沒開放任何模型");
  const price = (v) => el("td", { class: "text-end font-monospace" }, v ?? "—");
  tbody.replaceChildren(...rows.map((m) => {
    const chat = m.kind === "chat";
    return el("tr", {},
      el("td", {}, el("code", {}, m.name)),
      el("td", {}, MODEL_KINDS[m.kind] || m.kind),
      // 輸入欄：對話、embeddings 是每 100 萬 tokens；語音是每 100 萬字元；圖片是每張
      price(m.kind === "image" ? `${m.image_price_usd} / 張` : m.kind === "speech" ? `${m.input_price_usd} / 百萬字元` : m.input_price_usd),
      price(m.cached_input_price_usd),
      price(m.output_price_usd),
      el("td", { class: "text-end" }, chat ? m.max_output_tokens.toLocaleString() : "—"),
      el("td", {}, !chat ? "—" : m.accepts_images ? badge("可以", "success") : badge("不接受", "secondary")),
    );
  }));
}

// ---------------------------------------------------------------- 用量紀錄、帳本

async function loadUsage() {
  const rows = await api("/api/usage?limit=200");
  const tbody = $("usage-list");
  if (!rows.length) return emptyRow(tbody, 8, "還沒有任何請求");
  tbody.replaceChildren(...rows.map((u) => el("tr", {},
    el("td", {}, fmtTime(u.created_at)),
    el("td", {}, u.model),
    el("td", {}, u.stream ? "是" : "否"),
    el("td", {}, statusBadge(USAGE_STATUS, u.status)),
    el("td", { class: "text-end" }, u.prompt_tokens ?? "-"),
    el("td", { class: "text-end" }, u.completion_tokens ?? "-"),
    el("td", { class: "text-end font-monospace" }, u.hold_usd),
    el("td", { class: "text-end font-monospace" }, u.cost_usd),
  )));
}

async function loadLedger() {
  const rows = await api("/api/ledger?limit=200");
  const tbody = $("ledger-list");
  if (!rows.length) return emptyRow(tbody, 4, "還沒有任何餘額變動");
  tbody.replaceChildren(...rows.map((e) => el("tr", {},
    el("td", {}, fmtTime(e.created_at)),
    el("td", {}, LEDGER_KIND[e.kind] || e.kind),
    el("td", { class: "text-end font-monospace " + (e.amount_usd.startsWith("-") ? "text-danger" : "text-success") },
      e.amount_usd),
    el("td", { class: "small text-body-secondary" }, e.ref ?? ""),
  )));
}

// ---------------------------------------------------------------- API Key

async function loadKeys() {
  const rows = await api("/api/keys");
  const tbody = $("key-list");
  if (!rows.length) return emptyRow(tbody, 5, "還沒有 key，先建立一把");
  tbody.replaceChildren(...rows.map((k) => el("tr", {},
    el("td", {}, k.name),
    el("td", {}, el("code", {}, k.key_prefix)),
    el("td", {}, k.is_active ? badge("使用中", "success") : badge("已停用", "secondary")),
    el("td", {}, fmtTime(k.created_at)),
    el("td", { class: "text-end" }, k.is_active
      ? el("button", { class: "btn btn-outline-danger btn-sm", type: "button", onclick: () => revokeKey(k) }, "停用")
      : null),
  )));
}

function revokeKey(key) {
  if (!confirm(`確定要停用「${key.name}」（${key.key_prefix}）？停用後無法恢復，用這把 key 的工具會立刻失效。`)) return;
  guarded(async () => {
    await api(`/api/keys/${key.id}`, { method: "DELETE" });
    showAlert("已停用", "success");
    await loadKeys();
  });
}

$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  guarded(async () => {
    const data = await api("/api/keys", { method: "POST", body: { name: form.name.value.trim() || "default" } });
    form.reset();
    $("new-key-value").value = data.key;
    bootstrap.Modal.getOrCreateInstance($("new-key-modal")).show();
    await loadKeys();
  });
});

// 關掉視窗後把 key 從頁面上清掉，避免留在畫面上被看到
$("new-key-modal").addEventListener("hidden.bs.modal", () => { $("new-key-value").value = ""; });

// ---------------------------------------------------------------- 儲值

let depositOptions = null;
let currentOrder = null;
let pollTimer = null;
let countdownTimer = null;

async function loadDeposit() {
  if (!depositOptions) depositOptions = await api("/api/deposits/options");
  const opts = depositOptions;
  $("deposit-disabled").classList.toggle("d-none", opts.enabled);
  $("deposit-main").classList.toggle("d-none", !opts.enabled);
  if (opts.enabled) {
    const select = $("deposit-token");
    if (!select.options.length) {
      select.replaceChildren(...opts.tokens.map((t) => el("option", { value: t.symbol }, t.symbol)));
      const amount = $("deposit-amount");
      amount.min = opts.min_usd;
      amount.max = opts.max_usd;
      $("deposit-range").textContent =
        `${opts.min_usd} ~ ${opts.max_usd} 美元；訂單 ${opts.ttl_minutes} 分鐘內要付款`;
    }
  }
  await refreshDepositList();
}

async function refreshDepositList() {
  const rows = await api("/api/deposits");
  const tbody = $("deposit-list");
  if (currentOrder) {
    const latest = rows.find((o) => o.id === currentOrder.id);
    if (latest) showOrder(latest);
  }
  if (!rows.length) return emptyRow(tbody, 6, "還沒有儲值紀錄");
  tbody.replaceChildren(...rows.map((o) => el("tr", {},
    el("td", {}, o.id),
    el("td", {}, o.token),
    el("td", { class: "text-end font-monospace" }, o.pay_amount),
    el("td", {}, statusBadge(DEPOSIT_STATUS, o.status)),
    el("td", {}, o.status === "paid" ? `入帳 ${fmtTime(o.paid_at)}` : fmtTime(o.expires_at)),
    el("td", { class: "text-end" }, o.status === "pending"
      ? el("button", { class: "btn btn-outline-primary btn-sm", type: "button", onclick: () => showOrder(o) }, "查看付款資訊")
      : null),
  )));
}

function showOrder(order) {
  const wasPending = currentOrder && currentOrder.id === order.id && currentOrder.status === "pending";
  currentOrder = order;
  $("order-empty").classList.add("d-none");
  $("order-card").classList.remove("d-none");
  $("order-id").textContent = order.id;
  $("order-amount").textContent = order.pay_amount;
  $("order-token").textContent = order.token;
  $("order-address").textContent = order.to_address;
  $("order-chain").textContent = order.chain;
  $("order-contract").textContent = order.token_contract;
  const status = $("order-status");
  const [text, color] = DEPOSIT_STATUS[order.status] || [order.status, "secondary"];
  status.textContent = text;
  status.className = `badge text-bg-${color}`;
  $("order-paid").classList.toggle("d-none", order.status !== "paid");
  $("order-countdown-wrap").classList.toggle("d-none", order.status !== "pending");
  drawQr(order.to_address);

  if (order.status === "paid" && wasPending) {
    showAlert(`儲值 ${order.amount_usd} 美元已入帳`, "success");
    guarded(refreshMe);
  }
  startOrderTimers();
}

function drawQr(address) {
  const img = $("order-qr");
  if (typeof qrcode !== "function") {
    img.classList.add("d-none"); // QR 函式庫載不到時，還是可以手動複製地址
    return;
  }
  const qr = qrcode(0, "M");
  qr.addData(address);
  qr.make();
  img.src = qr.createDataURL(6, 8);
  img.classList.remove("d-none");
}

function startOrderTimers() {
  clearInterval(countdownTimer);
  clearInterval(pollTimer);
  if (!currentOrder || currentOrder.status !== "pending") return;
  const tick = () => {
    const left = new Date(currentOrder.expires_at) - Date.now();
    if (left <= 0) {
      $("order-countdown").textContent = "已超過付款期限，請不要再轉帳（如果剛剛已經轉了，系統還會再等一段時間）";
      return;
    }
    const min = Math.floor(left / 60000);
    const sec = Math.floor((left % 60000) / 1000);
    $("order-countdown").textContent = `${min} 分 ${String(sec).padStart(2, "0")} 秒`;
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
  // 待付款時定期查訂單狀態；離開儲值頁就停
  pollTimer = setInterval(() => {
    if (currentView() !== "deposit") return clearInterval(pollTimer);
    guarded(refreshDepositList);
  }, POLL_MS);
}

$("deposit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  guarded(async () => {
    const order = await api("/api/deposits", {
      method: "POST",
      body: { token: form.token.value, amount_usd: Number(form.amount_usd.value) },
    });
    showOrder(order);
    await refreshDepositList();
  });
});

// ---------------------------------------------------------------- 頁面切換

const LOADERS = {
  overview: loadOverview,
  deposit: loadDeposit,
  keys: loadKeys,
  models: loadModels,
  usage: loadUsage,
  ledger: loadLedger,
};

function currentView() {
  const name = location.hash.replace(/^#/, "");
  return name in LOADERS ? name : "overview";
}

function showSection(name) {
  for (const section of document.querySelectorAll("[data-view]")) {
    section.classList.toggle("d-none", section.dataset.view !== name);
  }
  for (const link of document.querySelectorAll("#nav-links .nav-link")) {
    link.classList.toggle("active", link.getAttribute("href") === "#" + name);
  }
}

/** 重設密碼連結長這樣：/#reset/<token>。token 放在 # 後面，瀏覽器不會把它送到伺服器或其他網站。 */
function resetToken() {
  const match = location.hash.match(/^#reset\/([A-Za-z0-9_-]{1,128})$/);
  return match ? match[1] : null;
}

$("reset-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  if (form.password.value !== form.confirm.value) {
    showAlert("兩次輸入的密碼不一樣");
    return;
  }
  guarded(async () => {
    await api("/api/password-reset", { method: "POST", body: { token: resetToken(), password: form.password.value } });
    form.reset();
    setToken(null); // 舊的登入在伺服器端已經失效
    history.replaceState(null, "", location.pathname); // 把用過的 token 從網址列和瀏覽紀錄拿掉
    clearAlerts();
    route();
    showAlert("密碼已更新，請用新密碼登入", "success");
  });
});

function route() {
  if (resetToken()) {
    $("navbar").classList.add("d-none");
    showSection("reset");
    return;
  }
  if (!getToken()) {
    $("navbar").classList.add("d-none");
    currentOrder = null;
    depositOptions = null;
    clearInterval(pollTimer);
    clearInterval(countdownTimer);
    showSection("auth");
    return;
  }
  $("navbar").classList.remove("d-none");
  const name = currentView();
  showSection(name);
  // 手機版選單：點完就收起來
  const collapse = bootstrap.Collapse.getInstance($("nav-links"));
  if (collapse) collapse.hide();
  guarded(async () => {
    await refreshMe();
    await LOADERS[name]();
  });
}

// 依系統設定切換深色／淺色
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const applyTheme = () => { document.documentElement.dataset.bsTheme = darkQuery.matches ? "dark" : "light"; };
darkQuery.addEventListener("change", applyTheme);
applyTheme();

window.addEventListener("hashchange", route);
route();
