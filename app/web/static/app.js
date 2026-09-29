const $ = (id) => document.getElementById(id);

const PAGE_META = {
  control: {
    title: "Điều khiển bot",
    lede: "Chọn hành vi rồi Bắt đầu. Diamond luôn khoá.",
  },
  wishlist: {
    title: "Wishlist",
    lede: "Hai crop mỗi item: shop có màu, báo màu in. Bấm vào item để sửa hoặc xoá.",
  },
  library: {
    title: "Thư viện",
    lede: "Icon chụp từ shop/báo. Gán vào vật phẩm khi tạo hoặc sửa.",
  },
  crops: {
    title: "Cây trồng",
    lede: "Hạt trên khay khi trồng. Wheat mặc định dùng seed_wheat.png.",
  },
  templates: {
    title: "Template hệ thống",
    lede: "HUD, cột báo, shop — chỉ xem. Không xoá từ web.",
  },
  config: {
    title: "Cấu hình",
    lede: "Lưu vào data/config.json. Không bao giờ cho phép tiêu diamond.",
  },
  develop: {
    title: "Phát triển bot",
    lede: "Từng bước mua hàng trên báo, chụp màn, nhận diện, camera. Overlay debug nằm trong Cấu hình.",
  },
};
const LIVE_PAGES = new Set(["control", "develop"]);
const PATH_TO_PAGE = {
  "/": "control",
  "/control": "control",
  "/index.html": "control",
  "/wishlist": "wishlist",
  "/library": "library",
  "/crops": "crops",
  "/templates": "templates",
  "/config": "config",
  "/dev": "develop",
  "/develop": "develop",
};

let currentPage = "control";

function pageFromPath(pathname) {
  const path = (pathname || "/").replace(/\/$/, "") || "/";
  return PATH_TO_PAGE[path] || "control";
}

function showPage(name, push) {
  if (!PAGE_META[name]) name = "control";
  currentPage = name;
  document.body.dataset.page = name;
  document.body.classList.toggle("has-live", LIVE_PAGES.has(name));
  document.querySelectorAll(".page").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === name);
  });
  document.querySelectorAll("[data-nav]").forEach((el) => {
    const on = el.dataset.nav === name;
    el.classList.toggle("active", on);
    if (on) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  $("page-title").textContent = PAGE_META[name].title;
  $("page-lede").textContent = PAGE_META[name].lede;
  updateTitle();
  if (push) {
    const link = document.querySelector(`[data-nav="${name}"]`);
    const href = (link && link.getAttribute("href")) || "/control";
    if (location.pathname !== href) history.pushState({ page: name }, "", href);
  }
  if (name === "wishlist") renderWishlist();
}

document.querySelector(".nav").addEventListener("click", (ev) => {
  const link = ev.target.closest("[data-nav]");
  if (!link) return;
  ev.preventDefault();
  showPage(link.dataset.nav, true);
});
window.addEventListener("popstate", () => showPage(pageFromPath(location.pathname), false));

// Per-browser view choices only (filters, folds). Bot settings live on the server.
function localGet(key, fallback) {
  try {
    const raw = localStorage.getItem(`farmbot.${key}`);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (err) {
    return fallback;
  }
}

function localSet(key, value) {
  try {
    localStorage.setItem(`farmbot.${key}`, JSON.stringify(value));
  } catch (err) {
    /* private mode: the choice just is not remembered */
  }
}

function toast(message, err = false) {
  if (!message) return;
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = `toast${err ? " err" : ""}`;
  el.setAttribute("role", err ? "alert" : "status");
  const text = document.createElement("span");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-x";
  close.setAttribute("aria-label", "Đóng");
  close.textContent = "×";
  el.append(text, close);
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    el.classList.add("out");
    setTimeout(() => el.remove(), 200);
  };
  close.addEventListener("click", dismiss);
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(dismiss, err ? 8000 : 3500);
}

function confirmDialog({ title = "Xác nhận", message = "", ok = "Xoá", danger = true } = {}) {
  const dlg = $("confirm-modal");
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  const okBtn = $("confirm-ok");
  okBtn.textContent = ok;
  okBtn.className = danger ? "btn danger solid" : "btn primary";
  dlg.returnValue = "";
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    // Focus lands on Huỷ (first button): Enter alone never deletes.
    dlg.showModal();
  });
}

async function api(url, options) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    throw new Error("Web API cũ hoặc không phải JSON — tắt tab cũ, mở lại đúng cổng");
  }
  if (!res.ok) {
    const error = new Error(data.error === "bot is running" ? "Bot đang chạy — bấm Dừng trước" : data.error || res.statusText);
    error.status = res.status;
    throw error;
  }
  return data;
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Không đọc được ảnh"));
    reader.readAsDataURL(file);
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------- time

// Server wall clock minus ours: a phone with a drifting clock still counts the
// rest down to the second the server will end it.
let clockOffset = 0;

function serverNow() {
  return Date.now() + clockOffset;
}

function ts(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t) ? t : 0;
}

function relTime(ms) {
  if (!ms) return "";
  const s = Math.max(0, Math.round((serverNow() - ms) / 1000));
  if (s < 45) return "vừa xong";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} phút trước`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} giờ trước`;
  return `${Math.round(h / 24)} ngày trước`;
}

function fmtDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function formatBuyTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso || "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())} ${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()}`;
}

// ---------------------------------------------------------------- config

let configDirty = false;

function setConfigDirty(on) {
  configDirty = on;
  $("config-dirty").hidden = !on;
  $("config-reset").disabled = !on;
}

// A refresh after some other action must not wipe half-typed settings.
function fillConfig(cfg, force = false) {
  if (!cfg || (configDirty && !force)) return;
  $("adb_host").value = cfg.adb_host || "";
  $("adb_port").value = cfg.adb_port ?? "";
  $("adb_bin").value = cfg.adb_bin || "";
  $("package").value = cfg.package || "";
  $("template_threshold").value = cfg.template_threshold;
  $("buy_threshold").value = cfg.buy_threshold ?? 0.72;
  $("news_threshold").value = cfg.news_threshold ?? 0.72;
  $("action_wait_s").value = cfg.action_wait_s ?? 0.9;
  $("buy_wait_s").value = cfg.buy_wait_s ?? 2;
  $("visit_wait_s").value = cfg.visit_wait_s ?? 2.5;
  $("poll_interval_s").value = cfg.poll_interval_s ?? 0;
  $("stall_swipe_ms").value = cfg.stall_swipe_ms ?? 280;
  $("swipe_duration_ms").value = cfg.swipe_duration_ms;
  $("debug").checked = Boolean(cfg.debug);
  setConfigDirty(false);
}

$("config-form").addEventListener("input", () => setConfigDirty(true));
$("config-form").addEventListener("change", () => setConfigDirty(true));
$("config-reset").addEventListener("click", () => fillConfig(lastState.config, true));

// ---------------------------------------------------------------- run prefs

let prefsTimer = 0;
let prefsPending = false;

function currentPrefs() {
  const limit = Number($("run-limit").value);
  const rest = $("run-rest-min").value;
  return {
    harvest: $("bh").checked,
    plant: $("bp").checked,
    newspaper: $("bn").checked,
    news_mode: $("news-mode").value,
    crop: $("run-crop").value || "wheat",
    limit: Number.isFinite(limit) && limit > 0 ? limit : 1,
    loop_rest_min: rest === "" ? undefined : Number(rest),
  };
}

function fillCropOptions(crops, selected) {
  const sel = $("run-crop");
  const keep = selected || sel.value;
  const ids = (crops || []).map((crop) => crop.id);
  if (keep && !ids.includes(keep)) ids.push(keep);
  if (!ids.length) ids.push("wheat");
  sel.innerHTML = ids.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
  sel.value = ids.includes(keep) ? keep : ids[0];
}

function syncPrefsUi() {
  $("run-crop").disabled = !$("bp").checked;
  $("news-mode").disabled = !$("bn").checked;
  // Rest only happens once every shop on the paper was visited.
  $("run-rest-min").disabled = !$("bn").checked;
}

function fillRunPrefs(prefs) {
  if (!prefs || prefsPending) return;
  $("bh").checked = Boolean(prefs.harvest);
  $("bp").checked = Boolean(prefs.plant);
  $("bn").checked = Boolean(prefs.newspaper);
  $("news-mode").value = prefs.news_mode || "sweep";
  fillCropOptions(lastState.crops, prefs.crop);
  const focused = document.activeElement;
  if (focused !== $("run-limit")) $("run-limit").value = prefs.limit;
  if (focused !== $("run-rest-min")) $("run-rest-min").value = prefs.loop_rest_min;
  syncPrefsUi();
}

function queuePrefsSave() {
  prefsPending = true;
  syncPrefsUi();
  $("prefs-status").textContent = "Đang lưu…";
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(savePrefs, 400);
}

async function savePrefs() {
  clearTimeout(prefsTimer);
  if (!prefsPending) return;
  try {
    const res = await api("/api/run-prefs", { method: "PUT", body: JSON.stringify(currentPrefs()) });
    lastState.run_prefs = res.run_prefs;
    prefsPending = false;
    fillRunPrefs(res.run_prefs);
    $("prefs-status").textContent = "Đã lưu ✓";
  } catch (err) {
    prefsPending = false;
    $("prefs-status").textContent = "";
    toast(`Không lưu được tuỳ chọn: ${err.message}`, true);
  }
}

$("run-prefs").addEventListener("change", queuePrefsSave);
$("run-prefs").addEventListener("input", (ev) => {
  if (ev.target.type === "number") queuePrefsSave();
});

// ---------------------------------------------------------------- cards

// Bumped only when item images may have changed, so re-rendering the grid
// reuses the images already loaded instead of refetching all of them.
let imgVersion = Date.now();

function templateUrl(name) {
  return `/api/templates/${encodeURIComponent(name)}.png?v=${imgVersion}`;
}

function missingLabel(item) {
  if (item.has_image && item.has_news_image) return "";
  if (item.has_image) return "thiếu báo";
  if (item.has_news_image) return "thiếu shop";
  return "thiếu ảnh";
}

function itemCard(item) {
  const id = escapeHtml(item.id);
  const shop = item.has_image
    ? `<img src="${templateUrl(item.template)}" alt="shop ${id}" loading="lazy" />`
    : `<div class="ph">Thiếu shop</div>`;
  const news = item.has_news_image
    ? `<img src="${templateUrl(item.news_template)}" alt="báo ${id}" loading="lazy" />`
    : `<div class="ph">Thiếu báo</div>`;
  const missing = missingLabel(item);
  const stat = buyIndex[item.id] || {};
  const buys = Number(stat.buy_count) || 0;
  const badges = [
    missing ? `<span class="badge bad">${missing}</span>` : "",
    buys ? `<span class="badge ok" title="Lần mua thành công">đã mua ${buys}</span>` : "",
  ].join("");
  return `<article class="tile item-tile${item.enabled ? "" : " off"}" tabindex="0" data-id="${id}" data-template="${escapeHtml(item.template)}" data-news-template="${escapeHtml(item.news_template)}" data-has-image="${item.has_image ? "1" : ""}" data-has-news="${item.has_news_image ? "1" : ""}" title="Bấm để sửa ${id}">
    <div class="pair">
      <div><span class="thumb-cap">Shop</span>${shop}</div>
      <div><span class="thumb-cap">Báo</span>${news}</div>
    </div>
    <div class="name">${id}</div>
    <div class="tile-foot">
      <div class="badges">${badges}</div>
      <label class="switch" title="Bật/tắt mua ${id}">
        <input type="checkbox" data-toggle="${id}" ${item.enabled ? "checked" : ""} />
        <span class="track" aria-hidden="true"></span>
        <span class="switch-label">Mua</span>
      </label>
    </div>
  </article>`;
}

function cropCard(crop) {
  const img = crop.has_image
    ? `<img src="${templateUrl(crop.seed_template)}" alt="${escapeHtml(crop.id)}" />`
    : `<div class="ph">Thiếu hạt<br>${escapeHtml(crop.seed_template)}.png</div>`;
  return `<article class="tile">
    ${img}
    <div class="name">${escapeHtml(crop.id)}</div>
    <div class="meta">${escapeHtml(crop.storage)} · ${escapeHtml(crop.seed_template)}</div>
    <div class="row">
      <button class="btn danger btn-compact" type="button" data-del-crop="${escapeHtml(crop.id)}">Xoá</button>
    </div>
  </article>`;
}

function templateCard(tpl) {
  return `<article class="tile">
    <img src="${templateUrl(tpl.name)}" alt="${escapeHtml(tpl.name)}" loading="lazy" />
    <div class="name">${escapeHtml(tpl.name)}</div>
    <div class="meta">${escapeHtml(tpl.kind)}${tpl.width ? ` · ${tpl.width}×${tpl.height}` : ""}</div>
  </article>`;
}

let lastState = {};
let libraryFilter = localGet("libFilter", "all");
let pickerSlot = null;

function libraryUrl(id) {
  return `/api/library/${encodeURIComponent(id)}.png`;
}

function libraryCard(row, { pick = false } = {}) {
  const kindLabel = row.kind === "news" ? "Báo" : "Shop";
  const size = row.width ? ` · ${row.width}×${row.height}` : "";
  const action = pick
    ? ""
    : `<div class="row"><button class="btn danger btn-compact" type="button" data-del-lib="${escapeHtml(row.id)}">Xoá</button></div>`;
  const pickAttr = pick ? ` data-pick-lib="${escapeHtml(row.id)}" tabindex="0"` : "";
  return `<article class="tile"${pickAttr}>
    <img src="${libraryUrl(row.id)}" alt="${escapeHtml(row.id)}" loading="lazy" />
    <div class="name">${escapeHtml(row.id)}</div>
    <div class="meta">${kindLabel}${size}</div>
    ${action}
  </article>`;
}

function renderLibrary(rows) {
  const list = rows || [];
  const shown = libraryFilter === "all" ? list : list.filter((row) => row.kind === libraryFilter);
  $("library").innerHTML = shown.length
    ? shown.map((row) => libraryCard(row)).join("")
    : `<p class="empty">Chưa có icon. Chụp từ Phát triển hoặc tải PNG.</p>`;
  document.querySelectorAll("[data-lib-filter]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.libFilter === libraryFilter);
  });
}

// ---------------------------------------------------------------- wishlist

let wishFilter = localGet("wishFilter", "all");
let wishSort = localGet("wishSort", "name");
let wishQuery = "";
let buyIndex = {};

function hasMissing(item) {
  return !item.has_image || !item.has_news_image;
}

function wishMatches(item) {
  if (wishQuery && !item.id.includes(wishQuery)) return false;
  if (wishFilter === "on") return item.enabled;
  if (wishFilter === "off") return !item.enabled;
  if (wishFilter === "missing") return hasMissing(item);
  return true;
}

function visibleWishlist() {
  const stat = (id) => buyIndex[id] || {};
  const byName = (a, b) => a.id.localeCompare(b.id);
  const sorters = {
    name: byName,
    buys: (a, b) => (Number(stat(b.id).buy_count) || 0) - (Number(stat(a.id).buy_count) || 0) || byName(a, b),
    recent: (a, b) => ts(stat(b.id).last_buy_at) - ts(stat(a.id).last_buy_at) || byName(a, b),
    enabled: (a, b) => Number(b.enabled) - Number(a.enabled) || byName(a, b),
  };
  return (lastState.wishlist || []).filter(wishMatches).sort(sorters[wishSort] || byName);
}

function renderWishlist() {
  const all = lastState.wishlist || [];
  const counts = {
    all: all.length,
    on: all.filter((item) => item.enabled).length,
    off: all.filter((item) => !item.enabled).length,
    missing: all.filter(hasMissing).length,
  };
  document.querySelectorAll("[data-count]").forEach((el) => {
    el.textContent = counts[el.dataset.count];
  });
  document.querySelectorAll("[data-wish-filter]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.wishFilter === wishFilter);
  });
  $("wish-sort").value = wishSort;
  $("wish-count").textContent = all.length ? `${counts.on}/${counts.all} đang mua` : "";
  const shown = visibleWishlist();
  $("wish-shown").textContent = all.length ? `Hiện ${shown.length} / ${all.length} item` : "";
  document.querySelectorAll("[data-bulk]").forEach((btn) => {
    btn.disabled = !shown.length;
  });
  if (!all.length) {
    // lastState.wishlist is only a real (empty) list once the server answered.
    if (!Array.isArray(lastState.wishlist)) return;
    $("items").innerHTML = `<p class="empty">Chưa có item. Mở “Thêm vật phẩm” ở trên, crop icon từ shop rồi thêm <code>wheat</code>.</p>`;
    $("item-add").open = true;
    return;
  }
  $("items").innerHTML = shown.length
    ? shown.map(itemCard).join("")
    : `<p class="empty">Không có item khớp bộ lọc.</p>`;
}

$("wish-search").addEventListener("input", (ev) => {
  wishQuery = ev.target.value.trim().toLowerCase().replace(/[\s-]+/g, "_");
  renderWishlist();
});
$("wish-sort").addEventListener("change", (ev) => {
  wishSort = ev.target.value;
  localSet("wishSort", wishSort);
  renderWishlist();
});

async function bulkToggle(on) {
  const ids = visibleWishlist()
    .filter((item) => item.enabled !== on)
    .map((item) => item.id);
  if (!ids.length) {
    toast(on ? "Các item đang hiện đều đã bật" : "Các item đang hiện đều đã tắt");
    return;
  }
  const ok = await confirmDialog({
    title: on ? "Bật mua hàng loạt?" : "Tắt mua hàng loạt?",
    message: `${on ? "Bật" : "Tắt"} mua ${ids.length} item đang hiện: ${ids.slice(0, 8).join(", ")}${ids.length > 8 ? "…" : ""}`,
    ok: `${on ? "Bật" : "Tắt"} ${ids.length} item`,
    danger: !on,
  });
  if (!ok) return;
  try {
    const state = await api("/api/items/bulk", {
      method: "POST",
      body: JSON.stringify({ ids, enabled: on }),
    });
    render(state);
    toast(`Đã ${on ? "bật" : "tắt"} ${state.changed} item`);
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------- render

function render(state) {
  if (!state) return;
  lastState = { ...lastState, ...state };
  fillConfig(state.config);
  if (state.crops) {
    $("crops").innerHTML = state.crops.length
      ? state.crops.map(cropCard).join("")
      : `<p class="empty">Chưa có cây trồng.</p>`;
    if (!prefsPending) fillCropOptions(state.crops, $("run-crop").value);
  }
  if (state.run_prefs) fillRunPrefs(state.run_prefs);
  if (Array.isArray(state.wishlist_buys)) renderWishlistBuys(state.wishlist_buys);
  if (state.wishlist) renderWishlist();
  if (state.library) {
    renderLibrary(state.library);
    if (pickerSlot) renderPickerGrid();
  }
  if (state.templates) {
    const system = state.templates.filter((t) => t.kind === "system");
    $("templates").innerHTML = system.map(templateCard).join("");
  }
  if (state.run) renderRun(state.run);
}

async function refresh() {
  render(await api("/api/state"));
}

// ---------------------------------------------------------------- polling

let pollTimer = 0;
let serverOnline = true;

function pollDelay() {
  const busy = lastRun && lastRun.status === "running";
  // A hidden tab still polls (slowly) so it can raise the error notification.
  if (document.hidden) return busy ? 5000 : 15000;
  return busy ? 1000 : 3000;
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollStatus, delay ?? pollDelay());
}

async function pollStatus() {
  try {
    const status = await api("/api/status");
    if (!serverOnline) {
      serverOnline = true;
      toast("Đã kết nối lại server web");
      // The server may have restarted: config, prefs and lists could differ.
      await refresh().catch(() => {});
    }
    renderRun(status.run);
    renderWishlistBuys(status.wishlist_buys);
  } catch (err) {
    if (serverOnline) {
      serverOnline = false;
      toast("Mất kết nối server web — đang thử lại…", true);
    }
    renderDevicePill();
  }
  schedulePoll();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) schedulePoll(0);
});

// ---------------------------------------------------------------- run state

const RUN_LABELS = {
  loop: "Vòng lặp",
  connect: "Kết nối",
  screenshot: "Chụp màn hình",
  detect: "Nhận diện",
  back: "Back",
  home: "Home",
  pan: "Pan camera",
  harvest: "Thu hoạch",
  plant: "Trồng",
  newspaper: "Mua trên báo",
  restart_game: "Khởi động lại game",
  go_home: "Về nhà",
  find_column: "Tìm cột báo",
  open_newspaper: "Mở báo",
  scan_ads: "Quét tin",
  swipe_newspaper: "Lật trang",
  visit_shop: "Vào shop",
  buy_wishlist: "Mua wishlist",
  close_shop: "Đóng shop",
  buy_shop: "Nhận diện & mua",
  capture_shop: "Lưu icon shop",
  capture_news: "Lưu icon tin báo",
};
const PHASE_LABELS = {
  home: "về nhà",
  harvest: "thu hoạch",
  plant: "trồng",
  newspaper: "ghé shop trên báo",
  rest: "nghỉ giữa vòng",
};
// Jobs that finish too often to toast each time.
const QUIET_JOBS = new Set(["screenshot", "pan"]);
// Jobs that add icons to the library: reload the lists once they finish.
const REFRESH_AFTER = new Set(["capture_shop", "capture_news"]);

let lastRun = null;
let lastFrameSeq = -1;
let lastLogText = "";
let sessionBuys = 0;

function jobLabel(job) {
  return RUN_LABELS[job] || job || "";
}

function renderDevicePill() {
  const device = $("device-pill");
  if (!serverOnline) {
    device.textContent = "Mất kết nối server";
    device.className = "pill err";
    return;
  }
  const run = lastRun || {};
  device.textContent = run.connected ? run.serial || "Đã kết nối" : "Chưa kết nối";
  device.className = `pill${run.connected ? " live" : ""}`;
}

function renderRun(run) {
  if (!run) return;
  if (typeof run.now === "number") clockOffset = run.now * 1000 - Date.now();
  const prev = lastRun;
  lastRun = run;
  renderDevicePill();

  const pill = $("run-pill");
  const busy = run.status === "running";
  if (run.stopping) {
    pill.textContent = "Đang dừng…";
    pill.className = "pill warn";
  } else if (busy && run.rest_until) {
    pill.textContent = "⏸ Nghỉ giữa vòng";
    pill.className = "pill live";
  } else if (busy) {
    pill.textContent = `▶ ${jobLabel(run.job)}`;
    pill.className = "pill live";
  } else if (run.status === "error") {
    pill.textContent = "⚠ Lỗi";
    pill.className = "pill err";
  } else {
    pill.textContent = "Sẵn sàng";
    pill.className = "pill idle";
  }

  document.querySelectorAll("[data-run], [data-pan]").forEach((btn) => {
    btn.disabled = busy;
  });
  $("btn-start").disabled = busy;
  $("btn-stop").disabled = !busy || Boolean(run.stopping);

  const note = [];
  if (run.screen) note.push(`Màn: ${run.screen}`);
  if (run.last_result) {
    const r = run.last_result;
    note.push(`${r.action} ${r.ok ? "ok" : "fail"} ${r.target || ""} ${r.error || ""}`.trim());
  }
  if (run.last_error) note.push(run.last_error);
  $("run-note").textContent = note.join(" · ");

  renderLog(run.logs);
  renderTiming(run.timing);
  if (run.has_frame && run.frame_seq !== lastFrameSeq) {
    lastFrameSeq = run.frame_seq;
    $("live-frame").src = `/api/frame.png?seq=${run.frame_seq ?? Date.now()}`;
    $("live-link").hidden = false;
    $("live-empty").hidden = true;
  }
  renderRunStatus();
  renderFrameAge();
  handleTransition(prev, run);
  updateTitle();
}

function renderRunStatus() {
  const run = lastRun;
  if (!run) return;
  const busy = run.status === "running";
  const resting = busy && run.rest_until;
  const box = $("run-status");
  let cls = "idle";
  let title = "Sẵn sàng";
  let sub = run.finished_at
    ? `Lần chạy trước kết thúc ${relTime(run.finished_at * 1000)}.`
    : "Chọn hành vi rồi bấm Bắt đầu.";
  if (run.stopping) {
    cls = "stopping";
    title = "Đang dừng…";
    sub = "Đợi bước hiện tại xong rồi dừng.";
  } else if (busy && run.job === "loop") {
    cls = resting ? "resting" : "live";
    title = `Vòng ${(run.rounds || 0) + (resting ? 0 : 1)} · ${PHASE_LABELS[run.phase] || "đang chạy"}`;
    sub = resting ? "Hết shop trên báo, nghỉ rồi chạy vòng mới." : "Bot đang tự chơi. Bấm Dừng để ngắt.";
  } else if (busy) {
    cls = "live";
    title = `Đang chạy: ${jobLabel(run.job)}`;
    sub = "Lệnh một lần — xong sẽ tự nghỉ.";
  } else if (run.status === "error") {
    cls = "err";
    title = `Lỗi${run.job ? ` · ${jobLabel(run.job)}` : ""}`;
    sub = run.last_error || "Xem nhật ký bên cạnh.";
  }
  box.className = `run-status ${cls}`;
  $("run-status-title").textContent = title;
  $("run-status-sub").textContent = sub;

  const started = run.started_at ? run.started_at * 1000 : 0;
  const ended = busy ? serverNow() : run.finished_at ? run.finished_at * 1000 : 0;
  $("stat-elapsed").textContent = started && ended ? fmtDuration(ended - started) : "—";
  $("stat-rounds").textContent = run.job === "loop" || run.rounds ? String(run.rounds || 0) : "—";
  $("stat-buys").textContent = started ? String(sessionBuys) : "—";

  const restBox = $("rest-box");
  restBox.hidden = !resting;
  if (resting) {
    const left = Math.max(0, run.rest_until * 1000 - serverNow());
    const total = Math.max(1, (run.rest_s || 1) * 1000);
    $("rest-left").textContent = fmtDuration(left + 999);
    $("rest-bar-fill").style.width = `${Math.min(100, Math.max(0, 100 - (left / total) * 100))}%`;
    // Read like the game's storage bar: "3:20 / 5:00".
    $("rest-bar-label").textContent = `${fmtDuration(left + 999)} / ${fmtDuration(total)}`;
  }
}

function renderFrameAge() {
  const run = lastRun;
  const el = $("frame-age");
  if (!run || !run.frame_at) {
    el.textContent = "";
    return;
  }
  const s = Math.max(0, Math.round((serverNow() - run.frame_at * 1000) / 1000));
  el.textContent = s < 60 ? `ảnh ${s}s trước` : `ảnh ${relTime(run.frame_at * 1000)}`;
}

setInterval(() => {
  renderRunStatus();
  renderFrameAge();
}, 1000);

function logClass(line) {
  if (/\bFAIL\b|error|lỗi|Traceback/i.test(line)) return "log-fail";
  if (/\b(ok|done)\b/.test(line)) return "log-ok";
  return "";
}

function renderLog(lines) {
  const el = $("run-log");
  const list = (lines || []).slice(-60);
  const text = list.join("\n");
  if (text === lastLogText) return;
  lastLogText = text;
  // Follow new lines only when already at the bottom: scrolling up to read
  // an older line must not get yanked away every second.
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  el.innerHTML = list.map((line) => `<span class="${logClass(line)}">${escapeHtml(line)}</span>`).join("\n");
  if (stick) el.scrollTop = el.scrollHeight;
}

function handleTransition(prev, run) {
  if (!prev || prev.status !== "running" || run.status === "running") return;
  const job = prev.job;
  if (REFRESH_AFTER.has(job)) refresh().catch(() => {});
  if (run.status === "error") {
    const msg = run.last_error || "Bot gặp lỗi";
    toast(`${jobLabel(job)} lỗi: ${msg}`, true);
    notify("Bot lỗi", msg);
    return;
  }
  if (job === "loop") {
    toast("Vòng lặp đã dừng");
    notify("Bot đã dừng", "Vòng lặp kết thúc.");
    return;
  }
  if (QUIET_JOBS.has(job)) return;
  const r = run.last_result;
  if (r && r.ok === false) {
    toast(`${jobLabel(job)} thất bại${r.error ? `: ${r.error}` : ""}`, true);
  } else if (job === "connect") {
    toast(`Đã kết nối ${run.serial || ""}`.trim());
  } else {
    toast(`${jobLabel(job)} xong`);
  }
}

// ---------------------------------------------------------------- title & notifications

const canNotify = "Notification" in window && window.isSecureContext;

function syncNotifyBtn() {
  $("btn-notify").hidden = !canNotify || Notification.permission !== "default";
}

function notify(title, body) {
  if (!canNotify || Notification.permission !== "granted" || !document.hidden) return;
  try {
    new Notification(title, { body, tag: "farmbot" });
  } catch (err) {
    /* some browsers only allow notifications from a service worker */
  }
}

$("btn-notify").addEventListener("click", async () => {
  try {
    const answer = await Notification.requestPermission();
    if (answer === "granted") toast("Sẽ báo khi bot lỗi hoặc dừng lúc tab đang ẩn");
  } catch (err) {
    toast("Trình duyệt không cho bật thông báo", true);
  }
  syncNotifyBtn();
});

// ---------------------------------------------------------------- share

function drawShareQr(url) {
  const canvas = $("share-qr");
  if (typeof qrcode !== "function") {
    canvas.hidden = true;
    return;
  }
  const qr = qrcode(0, "M");
  qr.addData(url);
  qr.make();
  const count = qr.getModuleCount();
  const quiet = 4;
  const cell = Math.floor(canvas.width / (count + quiet * 2));
  const size = cell * (count + quiet * 2);
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#000";
  for (let r = 0; r < count; r++) {
    for (let c = 0; c < count; c++) {
      if (qr.isDark(r, c)) ctx.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
    }
  }
  canvas.hidden = false;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    // Plain-HTTP pages are not a secure context, so the clipboard API can be missing.
    const box = document.createElement("textarea");
    box.value = text;
    document.body.appendChild(box);
    box.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    box.remove();
    return ok;
  }
}

async function openShare() {
  try {
    const info = await api("/api/share");
    const urls = info.urls || [];
    $("share-list").innerHTML = urls
      .map(
        (url, i) => `<li>
        <button type="button" class="share-url${i === 0 ? " active" : ""}" data-share-url="${escapeHtml(url)}">${escapeHtml(url)}</button>
        <button type="button" class="btn btn-compact" data-share-copy="${escapeHtml(url)}">Copy</button>
      </li>`
      )
      .join("");
    $("share-note").textContent = info.local_only
      ? "Server đang chỉ nghe localhost. Chạy web với --host 0.0.0.0 để chia sẻ."
      : urls.length
        ? ""
        : "Không tìm thấy địa chỉ mạng LAN. Kiểm tra kết nối Wi-Fi/LAN của máy tính.";
    if (urls.length) drawShareQr(urls[0]);
    else $("share-qr").hidden = true;
    $("share-modal").showModal();
  } catch (err) {
    toast(err.message, true);
  }
}

$("btn-share").addEventListener("click", openShare);
$("share-close").addEventListener("click", () => $("share-modal").close());
$("share-modal").addEventListener("click", async (ev) => {
  if (ev.target === $("share-modal")) return $("share-modal").close();
  const copy = ev.target.closest("[data-share-copy]");
  if (copy) {
    toast((await copyText(copy.dataset.shareCopy)) ? "Đã copy link" : "Không copy được, hãy chọn link thủ công", false);
    return;
  }
  const pick = ev.target.closest("[data-share-url]");
  if (pick) {
    document.querySelectorAll("[data-share-url]").forEach((el) => el.classList.toggle("active", el === pick));
    drawShareQr(pick.dataset.shareUrl);
  }
});

function updateTitle() {
  const run = lastRun;
  let prefix = "";
  let icon = "🌾";
  if (run && run.status === "running") {
    prefix = run.rest_until ? "⏸ " : "▶ ";
    icon = run.rest_until ? "⏸️" : "▶️";
  } else if (run && run.status === "error") {
    prefix = "⚠ ";
    icon = "⚠️";
  }
  document.title = `${prefix}Hay Day Bot — ${PAGE_META[currentPage].title}`;
  const href = `data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>${icon}</text></svg>`;
  const link = $("favicon");
  if (link.getAttribute("href") !== href) link.setAttribute("href", href);
}

// ---------------------------------------------------------------- timing

// Loop order, not alphabetical: the table reads like one shop visit.
const TIMING_ORDER = [
  "shop",
  "find_column",
  "open_newspaper",
  "next_listing",
  "visit_shop",
  "buy_wishlist",
  "find_slots",
  "buy_one",
  "stall_rewind",
  "stall_pan",
  "close_shop",
  "go_home",
];

function secs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "0";
  return n < 10 ? n.toFixed(2).replace(/0$/, "") : n.toFixed(1);
}

function renderTiming(timing) {
  const panel = $("timing-panel");
  if (!panel) return;
  const steps = (timing && timing.steps) || {};
  const names = Object.keys(steps);
  if (!names.length) {
    panel.hidden = true;
    $("timing-head").textContent = "chưa có số đo — chạy vòng báo";
    return;
  }
  panel.hidden = false;
  const head = [];
  if (timing.shop_n) head.push(`shop #${timing.shop_n}`);
  if (timing.per_shop_s) head.push(`${secs(timing.per_shop_s)}s/shop`);
  head.push(`phiên ${secs(timing.session_s)}s`);
  const shot = timing.shot || {};
  if (shot.n) head.push(`${shot.n} ảnh · ${secs(shot.avg_s)}s/ảnh`);
  if (timing.current) head.push(`đang: ${timing.current}`);
  $("timing-head").textContent = head.join(" · ");

  const split = timing.split_pct || {};
  $("timing-split").innerHTML = ["adb", "sleep", "cpu"]
    .map((key) => {
      const pct = Number(split[key]) || 0;
      const label = key === "adb" ? "ADB" : key === "sleep" ? "Chờ" : "CPU";
      return `<span class="seg seg-${key}" style="width:${pct}%" title="${label} ${pct}%">${
        pct >= 12 ? `${label} ${pct}%` : ""
      }</span>`;
    })
    .join("");

  const safety = timing.safety || {};
  const risky = (safety.verify_fail || 0) + (safety.visit_fail || 0);
  const safetyEl = $("timing-safety");
  safetyEl.classList.toggle("warn", risky > 0);
  safetyEl.textContent = [
    `mua ${safety.buys || 0}`,
    `verify fail ${safety.verify_fail || 0}`,
    `bỏ qua ${safety.buy_skip || 0}`,
    `vào shop lỗi ${safety.visit_fail || 0}`,
    `đụng trần ${safety.deadline_hits || 0}`,
  ].join(" · ");

  const ordered = [
    ...TIMING_ORDER.filter((name) => steps[name]),
    ...names.filter((name) => !TIMING_ORDER.includes(name)),
  ];
  $("timing-log").innerHTML = ordered
    .map((name) => {
      const s = steps[name];
      const cap = s.deadline_s;
      const hit = (s.deadline_hits || 0) > 0;
      // Finishing well inside the ceiling while still sleeping means the wait
      // can come down; hitting the ceiling means it must go up.
      const slack =
        !hit && cap && s.avg_s < cap * 0.6 && s.sleep_s > 0.05;
      const cls = hit ? "hot" : slack ? "slack" : "";
      return `<tr class="${cls}">
        <td class="step">${escapeHtml(name)}</td>
        <td>${cap == null ? "—" : `${secs(cap)}s`}</td>
        <td>${secs(s.last_s)}</td>
        <td>${secs(s.avg_s)}</td>
        <td>${secs(s.min_s)}–${secs(s.max_s)}</td>
        <td>${secs(s.adb_s)}</td>
        <td>${secs(s.sleep_s)}</td>
        <td>${secs(s.cpu_s)}</td>
        <td>${s.shots_avg ?? 0}</td>
        <td>${s.deadline_hits || 0}</td>
        <td>${s.n}</td>
      </tr>`;
    })
    .join("");

  const waits = timing.waits || {};
  $("timing-waits").textContent = Object.keys(waits).length
    ? `Cấu hình đang dùng — ${Object.entries(waits)
        .map(([key, value]) => `${key} ${value}`)
        .join(" · ")}`
    : "";
}

$("timing-details").open = Boolean(localGet("timingOpen", false));
$("timing-details").addEventListener("toggle", (ev) => localSet("timingOpen", ev.target.open));

// ---------------------------------------------------------------- buy history

let lastWishlistBuys = [];
let showAllBuys = Boolean(localGet("buyShowAll", false));
let buyLogKey = "";
let wishBuyKey = "";

function lastActivity(row) {
  return Math.max(ts(row.last_buy_at), ts(row.last_match_at));
}

function renderBuySummary() {
  const now = new Date(serverNow());
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const since = lastRun && lastRun.started_at ? lastRun.started_at * 1000 : 0;
  let buysToday = 0;
  let qtyToday = 0;
  let matchesToday = 0;
  let session = 0;
  for (const row of lastWishlistBuys) {
    for (const ev of row.buys || []) {
      const t = ts(ev.at);
      if (t >= dayStart) {
        buysToday += 1;
        qtyToday += Number(ev.qty) || 0;
      }
      if (since && t >= since) session += 1;
    }
    for (const ev of row.matches || []) {
      if (ts(ev.at) >= dayStart) matchesToday += 1;
    }
  }
  sessionBuys = session;
  $("buy-summary").innerHTML = `Hôm nay: <b>${buysToday}</b> lần mua${
    qtyToday ? ` (×${qtyToday})` : ""
  } · <b>${matchesToday}</b> lần khớp`;
}

function buyRow(row) {
  const src = row.has_image ? templateUrl(row.template) : row.has_news_image ? templateUrl(row.news_template) : "";
  const thumb = src ? `<img src="${escapeHtml(src)}" alt="" />` : `<span class="ph">?</span>`;
  const matches = Number(row.match_count) || 0;
  const buys = Number(row.buy_count) || 0;
  const qtySum = Number(row.qty_sum) || 0;
  const id = escapeHtml(row.id);
  const matchCell =
    matches > 0
      ? `<button type="button" class="buy-count" data-hist="match" data-id="${id}">${matches}</button>`
      : `<span class="buy-count zero">0</span>`;
  const buyCell =
    buys > 0
      ? `<button type="button" class="buy-count" data-hist="buy" data-id="${id}">${buys}</button>`
      : `<span class="buy-count zero">0</span>`;
  const lastBuy = ts(row.last_buy_at);
  const lastMatch = ts(row.last_match_at);
  const when = lastBuy ? `mua ${relTime(lastBuy)}` : lastMatch ? `khớp ${relTime(lastMatch)}` : "";
  const off = row.enabled === false ? `<span class="tag-off">tắt</span>` : "";
  return `<tr class="${row.enabled === false ? "off" : ""}">
    <td><div class="item">${thumb}<div class="item-text"><b>${id}</b>${off}<small>${escapeHtml(when)}</small></div></div></td>
    <td>${matchCell}</td>
    <td>${buyCell}</td>
    <td class="buy-qty-cell">${qtySum > 0 ? qtySum : `<span class="buy-count zero">0</span>`}</td>
  </tr>`;
}

function renderWishlistBuys(rows) {
  if (!Array.isArray(rows)) return;
  lastWishlistBuys = rows;
  buyIndex = Object.fromEntries(rows.map((row) => [row.id, row]));
  renderBuySummary();
  const counts = rows.map((r) => [r.id, r.enabled, r.match_count, r.buy_count, r.qty_sum, r.last_buy_at, r.last_match_at]);
  // Rebuild only on new data (or every 30 s for the "x phút trước" text).
  const key = JSON.stringify([counts, showAllBuys, Math.floor(Date.now() / 30000)]);
  if (key !== buyLogKey) {
    buyLogKey = key;
    const active = rows.filter((row) => row.match_count || row.buy_count).sort((a, b) => lastActivity(b) - lastActivity(a));
    const idle = showAllBuys ? rows.filter((row) => row.enabled !== false && !row.match_count && !row.buy_count) : [];
    const shown = [...active, ...idle];
    $("buy-log").innerHTML = shown.length
      ? shown.map(buyRow).join("")
      : `<tr><td colspan="4" class="muted">${
          showAllBuys ? "Chưa có item wishlist đang bật" : "Chưa khớp hay mua item nào. Tick “Cả item chưa mua” để xem hết."
        }</td></tr>`;
  }
  // Buy counts show on the wishlist cards too.
  const wishKey = JSON.stringify(counts);
  if (wishKey !== wishBuyKey) {
    wishBuyKey = wishKey;
    if (currentPage === "wishlist") renderWishlist();
  }
}

$("buy-show-all").checked = showAllBuys;
$("buy-show-all").addEventListener("change", (ev) => {
  showAllBuys = ev.target.checked;
  localSet("buyShowAll", showAllBuys);
  renderWishlistBuys(lastWishlistBuys);
});

function openBuyHistory(itemId, kind) {
  const row = lastWishlistBuys.find((entry) => entry.id === itemId);
  const events = row ? (kind === "buy" ? row.buys : row.matches) || [] : [];
  if (!row || !events.length) return;
  $("buy-history-title").textContent = kind === "buy" ? "Lần mua thành công" : "Lần khớp wishlist";
  const qtySum = kind === "buy" ? Number(row.qty_sum) || 0 : 0;
  $("buy-history-item").textContent = qtySum > 0 ? `${row.id} · Tổng ${qtySum}` : row.id;
  $("buy-history-list").innerHTML = events
    .map((ev) => {
      const time = escapeHtml(formatBuyTime(ev.at));
      const qty = Number(ev.qty) > 0 ? `×${Number(ev.qty)}` : "";
      const label = qty ? `${qty} · ${time}` : time;
      if (kind === "buy" && ev.image) {
        const src = `/api/buy-proofs/${encodeURIComponent(ev.image)}.png`;
        return `<li class="buy-proof"><a href="${escapeHtml(src)}" target="_blank" rel="noopener" title="Mở ảnh gốc"><img src="${escapeHtml(src)}" alt="" /></a><span>${escapeHtml(label)}</span></li>`;
      }
      return `<li>${escapeHtml(label)}</li>`;
    })
    .join("");
  $("buy-history-modal").showModal();
}

$("btn-buy-reset").addEventListener("click", async () => {
  const total = lastWishlistBuys.reduce((n, row) => n + (row.match_count || 0) + (row.buy_count || 0), 0);
  const ok = await confirmDialog({
    title: "Xoá lịch sử mua?",
    message: `Xoá ${total} lần khớp/mua và mọi ảnh bằng chứng mua. Không hoàn tác được.`,
    ok: "Xoá lịch sử",
  });
  if (!ok) return;
  try {
    const state = await api("/api/purchases", { method: "DELETE" });
    if (Array.isArray(state.wishlist_buys)) renderWishlistBuys(state.wishlist_buys);
    toast("Đã xóa lịch sử mua");
  } catch (err) {
    toast(err.message, true);
  }
});

$("buy-log").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-hist]");
  if (!btn || !$("buy-log").contains(btn)) return;
  openBuyHistory(btn.dataset.id, btn.dataset.hist);
});
$("buy-history-close").addEventListener("click", () => $("buy-history-modal").close());
$("buy-history-modal").addEventListener("click", (ev) => {
  if (ev.target === $("buy-history-modal")) $("buy-history-modal").close();
});

// ---------------------------------------------------------------- run commands

function runPayload(action, extra) {
  return {
    action,
    ...currentPrefs(),
    ...extra,
  };
}

async function sendRun(action, extra) {
  try {
    const state = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(runPayload(action, extra)),
    });
    renderRun(state.run);
    if (action === "loop") toast("Đã bắt đầu vòng lặp");
    // Fast jobs finish before the next regular poll: look again soon.
    schedulePoll(400);
  } catch (err) {
    toast(err.message, true);
    if (err.status === 409) schedulePoll(0);
  }
}

$("btn-start").addEventListener("click", async () => {
  const prefs = currentPrefs();
  if (!(prefs.harvest || prefs.plant || prefs.newspaper)) {
    toast("Chọn ít nhất một hành vi: Thu hoạch, Trồng hoặc Báo", true);
    return;
  }
  if (prefsPending) await savePrefs();
  await sendRun("loop");
});

$("btn-stop").addEventListener("click", async () => {
  try {
    const state = await api("/api/stop", { method: "POST", body: "{}" });
    renderRun(state.run);
    toast("Đã gửi lệnh dừng — bot dừng sau bước hiện tại");
    schedulePoll(400);
  } catch (err) {
    toast(err.message, true);
  }
});

$("btn-skip-rest").addEventListener("click", async () => {
  const btn = $("btn-skip-rest");
  btn.disabled = true;
  try {
    const state = await api("/api/skip-rest", { method: "POST", body: "{}" });
    renderRun(state.run);
    toast("Bỏ qua nghỉ — vòng mới bắt đầu");
    schedulePoll(400);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- clicks

document.body.addEventListener("click", async (ev) => {
  const runBtn = ev.target.closest("[data-run]");
  const panBtn = ev.target.closest("[data-pan]");
  if (runBtn) {
    ev.preventDefault();
    if (runBtn.dataset.confirm) {
      const ok = await confirmDialog({
        title: runBtn.textContent.trim(),
        message: runBtn.dataset.confirm,
        ok: runBtn.dataset.confirmOk || "Tiếp tục",
      });
      if (!ok) return;
    }
    await sendRun(runBtn.dataset.run, {
      ...(runBtn.dataset.newsMode ? { news_mode: runBtn.dataset.newsMode } : {}),
    });
    return;
  }
  if (panBtn) {
    ev.preventDefault();
    await sendRun("pan", { direction: panBtn.dataset.pan });
    return;
  }
  const delCrop = ev.target.closest("[data-del-crop]");
  const delLib = ev.target.closest("[data-del-lib]");
  const pickLib = ev.target.closest("[data-pick-lib]");
  const openPicker = ev.target.closest("[data-open-picker]");
  const libFilter = ev.target.closest("[data-lib-filter]");
  const wishFilterBtn = ev.target.closest("[data-wish-filter]");
  const bulkBtn = ev.target.closest("[data-bulk]");
  try {
    if (delLib) {
      const id = delLib.dataset.delLib;
      const ok = await confirmDialog({
        title: "Xoá icon khỏi thư viện?",
        message: `Xoá ${id}. Vật phẩm đã gán ảnh này vẫn giữ bản sao của nó.`,
      });
      if (!ok) return;
      render(await api(`/api/library/${encodeURIComponent(id)}`, { method: "DELETE" }));
      toast("Đã xoá khỏi thư viện");
      return;
    }
    if (delCrop) {
      const id = delCrop.dataset.delCrop;
      const ok = await confirmDialog({ title: "Xoá cây trồng?", message: `Xoá ${id} khỏi danh sách cây trồng.` });
      if (!ok) return;
      render(await api(`/api/crops/${encodeURIComponent(id)}`, { method: "DELETE" }));
      toast("Đã xoá cây trồng");
      return;
    }
    if (libFilter) {
      libraryFilter = libFilter.dataset.libFilter;
      localSet("libFilter", libraryFilter);
      renderLibrary(lastState.library || []);
      return;
    }
    if (wishFilterBtn) {
      wishFilter = wishFilterBtn.dataset.wishFilter;
      localSet("wishFilter", wishFilter);
      renderWishlist();
      return;
    }
    if (bulkBtn) {
      await bulkToggle(bulkBtn.dataset.bulk === "on");
      return;
    }
    if (openPicker) {
      ev.preventDefault();
      await showLibraryPicker(openPicker.dataset.openPicker);
      return;
    }
    if (pickLib) {
      applyLibraryPick(pickLib.dataset.pickLib);
      return;
    }
    const itemTile = ev.target.closest("#items .tile");
    if (itemTile && !ev.target.closest("button, input, label")) {
      openItemModal(itemTile);
    }
  } catch (err) {
    toast(err.message, true);
  }
});

document.body.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const tile = ev.target.closest("#items .tile, [data-pick-lib]");
  if (!tile || ev.target !== tile) return;
  ev.preventDefault();
  if (tile.dataset.pickLib) applyLibraryPick(tile.dataset.pickLib);
  else openItemModal(tile);
});

// ---------------------------------------------------------------- item forms

function slotIds(form, kind) {
  const edit = form === "edit";
  const shop = kind === "shop";
  return {
    hidden: $(edit ? (shop ? "item-edit-shop-lib" : "item-edit-news-lib") : shop ? "item-shop-lib" : "item-news-lib"),
    file: $(edit ? (shop ? "item-edit-file" : "item-edit-news-file") : shop ? "item-file" : "item-news-file"),
    img: $(edit ? (shop ? "item-edit-preview" : "item-edit-news-preview") : shop ? "item-preview-img" : "item-news-preview-img"),
  };
}

function parsePickerKey(key) {
  const [form, kind] = String(key || "").split("-");
  if ((form !== "add" && form !== "edit") || (kind !== "shop" && kind !== "news")) return null;
  return { form, kind };
}

function syncItemPreview() {
  const shop = $("item-file").files[0] || $("item-shop-lib").value;
  const news = $("item-news-file").files[0] || $("item-news-lib").value;
  $("item-preview").hidden = !(shop || news);
}

function applyLibraryPick(id) {
  if (!pickerSlot) return;
  const slot = slotIds(pickerSlot.form, pickerSlot.kind);
  slot.hidden.value = id;
  slot.file.value = "";
  slot.img.src = libraryUrl(id);
  slot.img.hidden = false;
  if (pickerSlot.form === "add") syncItemPreview();
  $("library-picker").close();
}

function renderPickerGrid() {
  if (!pickerSlot) return;
  const rows = (lastState.library || []).filter((row) => row.kind === pickerSlot.kind);
  $("library-picker-grid").innerHTML = rows.map((row) => libraryCard(row, { pick: true })).join("");
  $("library-picker-empty").hidden = rows.length > 0;
}

async function showLibraryPicker(key) {
  pickerSlot = parsePickerKey(key);
  if (!pickerSlot) return;
  $("library-picker-file").value = "";
  $("library-picker-title").textContent =
    pickerSlot.kind === "news" ? "Chọn ảnh báo từ thư viện" : "Chọn ảnh shop từ thư viện";
  if (!lastState.library) lastState = { ...lastState, ...(await api("/api/state")) };
  renderPickerGrid();
  $("library-picker").showModal();
}

$("library-picker-cancel").addEventListener("click", () => $("library-picker").close());
$("library-picker").addEventListener("click", (ev) => {
  if (ev.target === $("library-picker")) $("library-picker").close();
});
// Every way out (pick, Huỷ, ✖, Esc, backdrop) ends here.
$("library-picker").addEventListener("close", () => {
  pickerSlot = null;
});

// The round red ✖ on every popup.
document.addEventListener("click", (ev) => {
  const x = ev.target.closest(".modal-x");
  if (x) x.closest("dialog").close();
});

$("library-picker-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file || !pickerSlot) return;
  try {
    const state = await api("/api/library", {
      method: "POST",
      body: JSON.stringify({
        kind: pickerSlot.kind,
        image: await fileToDataUrl(file),
      }),
    });
    render(state);
    applyLibraryPick(state.saved.id);
    toast(state.saved.duplicate ? "Đã có icon giống, dùng lại" : "Đã thêm vào thư viện");
  } catch (err) {
    toast(err.message, true);
  }
});

async function previewFile(input, img, libInput, onDone) {
  const file = input.files[0];
  libInput.value = "";
  if (!file) {
    img.removeAttribute("src");
    img.hidden = true;
  } else {
    img.src = await fileToDataUrl(file);
    img.hidden = false;
  }
  if (onDone) onDone();
}

$("item-file").addEventListener("change", () =>
  previewFile($("item-file"), $("item-preview-img"), $("item-shop-lib"), syncItemPreview),
);
$("item-news-file").addEventListener("change", () =>
  previewFile($("item-news-file"), $("item-news-preview-img"), $("item-news-lib"), syncItemPreview),
);

$("item-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const shop = $("item-file").files[0];
    const news = $("item-news-file").files[0];
    const shopLib = $("item-shop-lib").value;
    const newsLib = $("item-news-lib").value;
    if (!shop && !news && !shopLib && !newsLib) throw new Error("Chọn ảnh shop hoặc ảnh báo");
    const body = { id: $("item-id").value, enabled: true };
    if (shop) body.image = await fileToDataUrl(shop);
    if (news) body.news_image = await fileToDataUrl(news);
    if (shopLib && !shop) body.shop_library = shopLib;
    if (newsLib && !news) body.news_library = newsLib;
    const state = await api("/api/items", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("item-form").reset();
    $("item-shop-lib").value = "";
    $("item-news-lib").value = "";
    $("item-preview").hidden = true;
    $("item-preview-img").hidden = true;
    $("item-news-preview-img").hidden = true;
    imgVersion = Date.now();
    render(state);
    toast(`Đã lưu ${state.item ? state.item.id : "item"}`);
  } catch (err) {
    toast(err.message, true);
  }
});

function openItemModal(tile) {
  const id = tile.dataset.id;
  $("item-edit-old").value = id;
  $("item-edit-id").value = id;
  $("item-edit-file").value = "";
  $("item-edit-news-file").value = "";
  $("item-edit-shop-lib").value = "";
  $("item-edit-news-lib").value = "";
  const shop = $("item-edit-preview");
  const news = $("item-edit-news-preview");
  if (tile.dataset.hasImage) {
    shop.src = templateUrl(tile.dataset.template);
    shop.hidden = false;
  } else {
    shop.removeAttribute("src");
    shop.hidden = true;
  }
  if (tile.dataset.hasNews) {
    news.src = templateUrl(tile.dataset.newsTemplate);
    news.hidden = false;
  } else {
    news.removeAttribute("src");
    news.hidden = true;
  }
  $("item-modal").showModal();
}

$("item-edit-cancel").addEventListener("click", () => $("item-modal").close());
$("item-modal").addEventListener("click", (ev) => {
  if (ev.target === $("item-modal")) $("item-modal").close();
});

$("item-edit-file").addEventListener("change", () => {
  if ($("item-edit-file").files[0]) previewFile($("item-edit-file"), $("item-edit-preview"), $("item-edit-shop-lib"));
});
$("item-edit-news-file").addEventListener("change", () => {
  if ($("item-edit-news-file").files[0]) previewFile($("item-edit-news-file"), $("item-edit-news-preview"), $("item-edit-news-lib"));
});

$("item-edit-delete").addEventListener("click", async () => {
  const id = $("item-edit-old").value;
  const ok = await confirmDialog({
    title: "Xoá vật phẩm?",
    message: `Xoá ${id} khỏi wishlist cùng 2 ảnh template của nó. Ảnh trong thư viện vẫn giữ.`,
  });
  if (!ok) return;
  try {
    const state = await api(`/api/items/${encodeURIComponent(id)}`, { method: "DELETE" });
    $("item-modal").close();
    render(state);
    toast(`Đã xoá ${id}`);
  } catch (err) {
    toast(err.message, true);
  }
});

$("item-edit-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const oldId = $("item-edit-old").value;
    const body = { id: $("item-edit-id").value };
    const shop = $("item-edit-file").files[0];
    const news = $("item-edit-news-file").files[0];
    const shopLib = $("item-edit-shop-lib").value;
    const newsLib = $("item-edit-news-lib").value;
    if (shop) body.image = await fileToDataUrl(shop);
    if (news) body.news_image = await fileToDataUrl(news);
    if (shopLib && !shop) body.shop_library = shopLib;
    if (newsLib && !news) body.news_library = newsLib;
    const state = await api(`/api/items/${encodeURIComponent(oldId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    $("item-modal").close();
    imgVersion = Date.now();
    render(state);
    toast("Đã cập nhật vật phẩm");
  } catch (err) {
    toast(err.message, true);
  }
});

$("library-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const file = $("library-file").files[0];
    if (!file) throw new Error("Chọn ảnh PNG");
    const state = await api("/api/library", {
      method: "POST",
      body: JSON.stringify({
        kind: $("library-kind").value,
        image: await fileToDataUrl(file),
      }),
    });
    $("library-form").reset();
    toast(state.saved.duplicate ? "Đã có icon giống, không thêm trùng" : "Đã thêm vào thư viện");
    render(state);
  } catch (err) {
    toast(err.message, true);
  }
});

$("crop-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const file = $("crop-file").files[0];
    const body = {
      id: $("crop-id").value,
      storage: $("crop-storage").value,
    };
    if (file) body.image = await fileToDataUrl(file);
    const state = await api("/api/crops", { method: "POST", body: JSON.stringify(body) });
    $("crop-form").reset();
    imgVersion = Date.now();
    render(state);
    toast("Đã lưu cây trồng");
  } catch (err) {
    toast(err.message, true);
  }
});

$("config-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const port = $("adb_port").value;
    await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({
        adb_host: $("adb_host").value,
        adb_port: port === "" ? null : Number(port),
        adb_bin: $("adb_bin").value,
        package: $("package").value,
        template_threshold: Number($("template_threshold").value),
        buy_threshold: Number($("buy_threshold").value),
        news_threshold: Number($("news_threshold").value),
        action_wait_s: Number($("action_wait_s").value),
        buy_wait_s: Number($("buy_wait_s").value),
        visit_wait_s: Number($("visit_wait_s").value),
        poll_interval_s: Number($("poll_interval_s").value),
        stall_swipe_ms: Number($("stall_swipe_ms").value),
        swipe_duration_ms: Number($("swipe_duration_ms").value),
        debug: $("debug").checked,
      }),
    });
    setConfigDirty(false);
    toast("Đã lưu cấu hình");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

document.body.addEventListener("change", async (ev) => {
  const toggle = ev.target.closest("[data-toggle]");
  if (!toggle) return;
  try {
    const state = await api(`/api/items/${encodeURIComponent(toggle.dataset.toggle)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: toggle.checked }),
    });
    render(state);
  } catch (err) {
    toast(err.message, true);
    toggle.checked = !toggle.checked;
  }
});

// ---------------------------------------------------------------- boot

showPage(pageFromPath(location.pathname), false);
syncNotifyBtn();
refresh()
  .then(() => schedulePoll())
  .catch((err) => {
    toast(err.message, true);
    schedulePoll();
  });
