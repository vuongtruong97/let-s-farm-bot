const $ = (id) => document.getElementById(id);

const PAGE_META = {
  control: {
    title: "Điều khiển bot",
    lede: "Chọn hành vi rồi Bắt đầu. Diamond luôn khoá.",
  },
  macro: {
    title: "Macro",
    lede: "Ghi tap/swipe trên ảnh live hoặc máy ảo, rồi phát lại toạ độ tuyệt đối.",
  },
  wishlist: {
    title: "Wishlist",
    lede: "Hai crop mỗi item: shop có màu, báo màu in. Chọn từ thư viện hoặc tải lên.",
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
const LIVE_PAGES = new Set(["control", "macro", "develop"]);
const PATH_TO_PAGE = {
  "/": "control",
  "/control": "control",
  "/index.html": "control",
  "/macro": "macro",
  "/wishlist": "wishlist",
  "/library": "library",
  "/crops": "crops",
  "/templates": "templates",
  "/config": "config",
  "/dev": "develop",
  "/develop": "develop",
};

function pageFromPath(pathname) {
  const path = (pathname || "/").replace(/\/$/, "") || "/";
  return PATH_TO_PAGE[path] || "control";
}

function showPage(name, push) {
  if (!PAGE_META[name]) name = "control";
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
  document.title = `Hay Day Bot — ${PAGE_META[name].title}`;
  if (push) {
    const link = document.querySelector(`[data-nav="${name}"]`);
    const href = (link && link.getAttribute("href")) || "/control";
    if (location.pathname !== href) history.pushState({ page: name }, "", href);
  }
}

document.querySelector(".nav").addEventListener("click", (ev) => {
  const link = ev.target.closest("[data-nav]");
  if (!link) return;
  ev.preventDefault();
  showPage(link.dataset.nav, true);
});
window.addEventListener("popstate", () => showPage(pageFromPath(location.pathname), false));
showPage(pageFromPath(location.pathname), false);

function toast(message, err = false) {
  const el = $("toast");
  el.hidden = !message;
  el.textContent = message || "";
  el.classList.toggle("err", Boolean(err));
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
    throw new Error(data.error || res.statusText);
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

function fillConfig(cfg) {
  $("adb_host").value = cfg.adb_host || "";
  $("adb_port").value = cfg.adb_port ?? "";
  $("adb_bin").value = cfg.adb_bin || "";
  $("package").value = cfg.package || "";
  $("template_threshold").value = cfg.template_threshold;
  $("buy_threshold").value = cfg.buy_threshold ?? 0.72;
  $("news_threshold").value = cfg.news_threshold ?? 0.72;
  $("loop_rest_min").value = cfg.loop_rest_min ?? 5;
  $("action_wait_s").value = cfg.action_wait_s ?? 0.9;
  $("buy_wait_s").value = cfg.buy_wait_s ?? 2;
  $("swipe_duration_ms").value = cfg.swipe_duration_ms;
  $("debug").checked = Boolean(cfg.debug);
  if ($("run-rest-min")) $("run-rest-min").value = cfg.loop_rest_min ?? 5;
}

function itemCard(item) {
  const shop = item.has_image
    ? `<img src="/api/templates/${item.template}.png?t=${Date.now()}" alt="shop ${item.id}" />`
    : `<div class="ph">Thiếu shop</div>`;
  const news = item.has_news_image
    ? `<img src="/api/templates/${item.news_template}.png?t=${Date.now()}" alt="báo ${item.id}" />`
    : `<div class="ph">Thiếu báo</div>`;
  const badge = item.has_image && item.has_news_image
    ? `<span class="badge ok">${item.width}×${item.height}</span>`
    : `<span class="badge bad">${item.has_image ? "thiếu báo" : item.has_news_image ? "thiếu shop" : "thiếu ảnh"}</span>`;
  return `<article class="tile" data-id="${item.id}" data-template="${item.template}" data-news-template="${item.news_template}" data-has-image="${item.has_image ? "1" : ""}" data-has-news="${item.has_news_image ? "1" : ""}" title="Bấm để sửa">
    <div class="pair">
      <div><span class="thumb-cap">Shop</span>${shop}</div>
      <div><span class="thumb-cap">Báo</span>${news}</div>
    </div>
    <div class="name">${item.id}</div>
    <div class="meta">${item.template} · ${item.news_template}</div>
    <div class="row">
      ${badge}
      <label class="check"><input type="checkbox" data-toggle="${item.id}" ${item.enabled ? "checked" : ""}/> Mua</label>
      <button class="btn danger" type="button" data-del-item="${item.id}">Xoá</button>
    </div>
  </article>`;
}

function cropCard(crop) {
  const img = crop.has_image
    ? `<img src="/api/templates/${crop.seed_template}.png?t=${Date.now()}" alt="${crop.id}" />`
    : `<div class="ph">Thiếu hạt<br>${crop.seed_template}.png</div>`;
  return `<article class="tile">
    ${img}
    <div class="name">${crop.id}</div>
    <div class="meta">${crop.storage} · ${crop.seed_template}</div>
    <div class="row">
      <button class="btn danger" type="button" data-del-crop="${crop.id}">Xoá</button>
    </div>
  </article>`;
}

function templateCard(tpl) {
  return `<article class="tile">
    <img src="/api/templates/${tpl.name}.png?t=${Date.now()}" alt="${tpl.name}" />
    <div class="name">${tpl.name}</div>
    <div class="meta">${tpl.kind}${tpl.width ? ` · ${tpl.width}×${tpl.height}` : ""}</div>
  </article>`;
}

let lastState = { library: [] };
let libraryFilter = "all";
let pickerSlot = null;

function libraryUrl(id) {
  return `/api/library/${id}.png?t=${Date.now()}`;
}

function libraryCard(row, { pick = false } = {}) {
  const kindLabel = row.kind === "news" ? "Báo" : "Shop";
  const size = row.width ? ` · ${row.width}×${row.height}` : "";
  const action = pick
    ? ""
    : `<div class="row"><button class="btn danger" type="button" data-del-lib="${row.id}">Xoá</button></div>`;
  const pickAttr = pick ? ` data-pick-lib="${row.id}"` : "";
  return `<article class="tile"${pickAttr}>
    <img src="${libraryUrl(row.id)}" alt="${row.id}" />
    <div class="name">${row.id}</div>
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

function render(state) {
  lastState = state || lastState;
  if (state.config) fillConfig(state.config);
  if (state.wishlist) {
    $("items").innerHTML = state.wishlist.length
      ? state.wishlist.map(itemCard).join("")
      : `<p class="empty">Chưa có item. Crop icon lúa mì từ shop rồi thêm <code>wheat</code>.</p>`;
  }
  if (state.library) renderLibrary(state.library);
  if (state.crops) {
    $("crops").innerHTML = state.crops.length
      ? state.crops.map(cropCard).join("")
      : `<p class="empty">Chưa có cây trồng.</p>`;
  }
  if (state.templates) {
    const system = state.templates.filter((t) => t.kind === "system");
    $("templates").innerHTML = system.map(templateCard).join("");
  }
  renderMacros(state);
  if (state.run) renderRun(state.run);
  if (Array.isArray(state.wishlist_buys)) renderWishlistBuys(state.wishlist_buys);
}

async function refresh() {
  render(await api("/api/state"));
}

let pollTimer = 0;
function schedulePoll(running) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const state = await api("/api/state");
      renderMacros(state);
      if (state.run) renderRun(state.run);
      if (Array.isArray(state.wishlist_buys)) renderWishlistBuys(state.wishlist_buys);
      const rec = state.run && state.run.recording && state.run.recording.active;
      schedulePoll(state.run && (state.run.status === "running" || rec));
    } catch (err) {
      schedulePoll(false);
    }
  }, running ? 800 : 2500);
}

function renderRun(run) {
  const device = $("device-pill");
  const pill = $("run-pill");
  device.textContent = run.connected ? run.serial || "Đã kết nối" : "Chưa kết nối";
  device.classList.toggle("live", Boolean(run.connected));
  let label = "Đang nghỉ";
  pill.className = "pill idle";
  if (run.stopping) {
    label = "Đang dừng…";
  } else if (run.status === "recording") {
    label = `Đang ghi: ${(run.recording && run.recording.name) || ""}`;
    pill.className = "pill live";
  } else if (run.status === "running") {
    label = `Đang chạy: ${run.job || ""}`;
    pill.className = "pill live";
  } else if (run.status === "error") {
    label = "Lỗi";
    pill.className = "pill err";
  }
  pill.textContent = label;
  const busy = run.status === "running";
  const rec = Boolean(run.recording && run.recording.active);
  $("btn-start").disabled = busy || rec;
  $("btn-stop").disabled = !busy;
  $("btn-record").disabled = busy || rec;
  $("btn-record-stop").disabled = !rec;
  $("btn-macro-play").disabled = busy || rec;
  $("btn-macro-back").disabled = !rec;
  $("viewport").classList.toggle("recording", rec);
  const note = [];
  if (run.screen) note.push(`Màn: ${run.screen}`);
  if (run.last_result) {
    const r = run.last_result;
    note.push(`${r.action} ${r.ok ? "ok" : "fail"} ${r.target || ""} ${r.error || ""}`.trim());
  }
  if (run.last_error) note.push(run.last_error);
  $("run-note").textContent = note.join(" · ");
  $("run-log").textContent = (run.logs || []).slice(-40).join("\n");
  if (run.has_frame) {
    $("live-frame").src = `/api/frame.png?t=${Date.now()}`;
    $("live-frame").hidden = false;
    $("live-empty").hidden = true;
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatBuyTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso || "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())} ${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()}`;
}

function renderWishlistBuys(rows) {
  const el = $("buy-log");
  if (!el) return;
  lastWishlistBuys = Array.isArray(rows) ? rows : [];
  if (!lastWishlistBuys.length) {
    el.innerHTML = `<tr><td colspan="3" class="muted">Chưa có item wishlist đang bật</td></tr>`;
    return;
  }
  el.innerHTML = lastWishlistBuys
    .map((row) => {
      const src = row.has_image
        ? `/api/templates/${row.template}.png?t=1`
        : row.has_news_image
          ? `/api/templates/${row.news_template}.png?t=1`
          : "";
      const thumb = src
        ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(row.id)}" />`
        : `<span class="ph">?</span>`;
      const matches = Number(row.match_count) || 0;
      const buys = Number(row.buy_count) || 0;
      const matchCell =
        matches > 0
          ? `<button type="button" class="buy-count" data-hist="match" data-id="${escapeHtml(row.id)}">${matches}</button>`
          : `<span class="buy-count zero">0</span>`;
      const buyCell =
        buys > 0
          ? `<button type="button" class="buy-count" data-hist="buy" data-id="${escapeHtml(row.id)}">${buys}</button>`
          : `<span class="buy-count zero">0</span>`;
      return `<tr>
        <td><div class="item">${thumb}<b>${escapeHtml(row.id)}</b></div></td>
        <td>${matchCell}</td>
        <td>${buyCell}</td>
      </tr>`;
    })
    .join("");
}

function openBuyHistory(itemId, kind) {
  const row = lastWishlistBuys.find((entry) => entry.id === itemId);
  const events = row ? (kind === "buy" ? row.buys : row.matches) || [] : [];
  if (!row || !events.length) return;
  $("buy-history-title").textContent = kind === "buy" ? "Lần mua thành công" : "Lần khớp wishlist";
  $("buy-history-item").textContent = row.id;
  $("buy-history-list").innerHTML = events
    .map((ev) => `<li>${escapeHtml(formatBuyTime(ev.at))}</li>`)
    .join("");
  $("buy-history-modal").showModal();
}

let lastStepsKey = "";
let lastMacroListKey = "";
let lastWishlistBuys = [];

function stepLabel(step) {
  if (step.type === "tap") return `tap ${step.x},${step.y}`;
  if (step.type === "swipe") {
    return `swipe ${step.x1},${step.y1} → ${step.x2},${step.y2} (${step.duration_ms}ms)`;
  }
  if (step.type === "wait") return "wait";
  return step.type;
}

function renderMacros(state) {
  const rec = (state.run && state.run.recording) || null;
  const active = Boolean(rec && rec.active);
  if (rec && rec.name && !$("macro-name").value) $("macro-name").value = rec.name;
  $("macro-note").textContent = active
    ? `Đang ghi ${rec.name} ${rec.width}×${rec.height} — click/kéo trên ảnh live hoặc chơi trên máy ảo`
    : "Nhập tên rồi Ghi. Phát lại macro đã lưu bên dưới.";
  const steps = (rec && rec.steps) || [];
  const stepsKey = JSON.stringify(steps);
  const editing = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.waitIdx != null;
  if (stepsKey !== lastStepsKey && !editing) {
    lastStepsKey = stepsKey;
    $("macro-steps").innerHTML = steps
      .map((step, i) => {
        const wait =
          step.type === "wait"
            ? `<input type="number" min="0" max="5000" data-wait-idx="${i}" value="${step.ms}" /> ms`
            : "";
        return `<li><span>${stepLabel(step)}</span>${wait}<button class="btn danger" type="button" data-del-step="${i}">Xoá</button></li>`;
      })
      .join("");
  }
  const list = state.macros || [];
  const listKey = JSON.stringify(list);
  if (listKey !== lastMacroListKey) {
    lastMacroListKey = listKey;
    $("macros").innerHTML = list.length
      ? list
          .map(
            (row) => `<article class="tile">
        <div class="name">${row.name}</div>
        <div class="meta">${row.steps} bước · ${row.width}×${row.height}</div>
        <div class="row">
          <button class="btn" type="button" data-play-macro="${row.id}">Phát</button>
          <button class="btn danger" type="button" data-del-macro="${row.id}">Xoá</button>
        </div>
      </article>`
          )
          .join("")
      : `<p class="empty">Chưa có macro.</p>`;
  }
}

function frameToDevice(img, clientX, clientY) {
  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth || 1920;
  const nh = img.naturalHeight || 1080;
  const scale = Math.min(rect.width / nw, rect.height / nh);
  const dispW = nw * scale;
  const dispH = nh * scale;
  const ox = rect.left + (rect.width - dispW) / 2;
  const oy = rect.top + (rect.height - dispH) / 2;
  const x = Math.round((clientX - ox) / scale);
  const y = Math.round((clientY - oy) / scale);
  return {
    x: Math.max(0, Math.min(nw - 1, x)),
    y: Math.max(0, Math.min(nh - 1, y)),
  };
}

function applyRunState(state) {
  renderMacros(state);
  if (state.run) renderRun(state.run);
  if (Array.isArray(state.wishlist_buys)) renderWishlistBuys(state.wishlist_buys);
}

function newsMode() {
  const el = document.querySelector("input[name=news-mode]:checked");
  return (el && el.value) || "follow";
}

function runPayload(action, extra) {
  return {
    action,
    harvest: $("bh").checked,
    plant: $("bp").checked,
    newspaper: $("bn").checked,
    news_mode: newsMode(),
    crop: $("run-crop").value || "wheat",
    limit: Number($("run-limit").value) || 1,
    loop_rest_min: Number($("run-rest-min").value),
    ...extra,
  };
}

const RUN_LABELS = {
  loop: "vòng lặp",
  go_home: "Về nhà",
  rest: "Nghỉ giữa vòng",
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

async function sendRun(action, extra) {
  try {
    const state = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(runPayload(action, extra)),
    });
    renderRun(state.run);
    toast(action === "loop" ? "Đã bắt đầu vòng lặp" : `Chạy ${RUN_LABELS[action] || action}`);
    if (action === "capture_shop" || action === "capture_news") {
      await refresh();
    }
    schedulePoll(state.run && state.run.status === "running");
  } catch (err) {
    toast(err.message, true);
  }
}

$("btn-start").addEventListener("click", () => sendRun("loop"));
$("btn-stop").addEventListener("click", async () => {
  try {
    const state = await api("/api/stop", { method: "POST", body: "{}" });
    renderRun(state.run);
    toast("Đã gửi lệnh dừng");
  } catch (err) {
    toast(err.message, true);
  }
});

$("btn-buy-reset").addEventListener("click", async () => {
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

$("btn-record").addEventListener("click", async () => {
  try {
    const state = await api("/api/macros/record/start", {
      method: "POST",
      body: JSON.stringify({ name: $("macro-name").value }),
    });
    lastStepsKey = "";
    applyRunState(state);
    toast("Đang ghi macro");
    schedulePoll(true);
  } catch (err) {
    toast(err.message, true);
  }
});

$("btn-record-stop").addEventListener("click", async () => {
  try {
    const state = await api("/api/macros/record/stop", { method: "POST", body: "{}" });
    lastStepsKey = "";
    lastMacroListKey = "";
    applyRunState(state);
    toast("Đã lưu macro");
    schedulePoll(false);
  } catch (err) {
    toast(err.message, true);
  }
});

$("btn-macro-play").addEventListener("click", () => {
  sendRun("macro", { name: $("macro-name").value });
});

$("btn-macro-back").addEventListener("click", async () => {
  try {
    const state = await api("/api/macros/gesture", {
      method: "POST",
      body: JSON.stringify({ type: "back" }),
    });
    lastStepsKey = "";
    applyRunState(state);
  } catch (err) {
    toast(err.message, true);
  }
});

let drag = null;
const live = $("live-frame");
live.addEventListener("pointerdown", (ev) => {
  if (!$("viewport").classList.contains("recording")) return;
  if (ev.button !== 0) return;
  ev.preventDefault();
  live.setPointerCapture(ev.pointerId);
  const pt = frameToDevice(live, ev.clientX, ev.clientY);
  drag = { ...pt, t: Date.now() };
});
live.addEventListener("pointerup", async (ev) => {
  if (!drag) return;
  ev.preventDefault();
  const end = frameToDevice(live, ev.clientX, ev.clientY);
  const start = drag;
  drag = null;
  const dist = Math.hypot(end.x - start.x, end.y - start.y);
  const duration_ms = Math.max(80, Date.now() - start.t);
  const body =
    dist < 12
      ? { type: "tap", x: start.x, y: start.y }
      : { type: "swipe", x1: start.x, y1: start.y, x2: end.x, y2: end.y, duration_ms };
  try {
    const state = await api("/api/macros/gesture", {
      method: "POST",
      body: JSON.stringify(body),
    });
    lastStepsKey = "";
    applyRunState(state);
  } catch (err) {
    toast(err.message, true);
  }
});
live.addEventListener("dragstart", (ev) => ev.preventDefault());

document.body.addEventListener("click", async (ev) => {
  const runBtn = ev.target.closest("[data-run]");
  const panBtn = ev.target.closest("[data-pan]");
  if (runBtn) {
    ev.preventDefault();
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
  const delItem = ev.target.closest("[data-del-item]");
  const delCrop = ev.target.closest("[data-del-crop]");
  const delMacro = ev.target.closest("[data-del-macro]");
  const delLib = ev.target.closest("[data-del-lib]");
  const pickLib = ev.target.closest("[data-pick-lib]");
  const openPicker = ev.target.closest("[data-open-picker]");
  const libFilter = ev.target.closest("[data-lib-filter]");
  const playMacro = ev.target.closest("[data-play-macro]");
  const delStep = ev.target.closest("[data-del-step]");
  try {
    if (playMacro) {
      $("macro-name").value = playMacro.dataset.playMacro;
      await sendRun("macro", { name: playMacro.dataset.playMacro });
      return;
    }
    if (delStep) {
      const state = await api(`/api/macros/record/steps/${delStep.dataset.delStep}`, {
        method: "DELETE",
      });
      lastStepsKey = "";
      applyRunState(state);
      return;
    }
    if (delMacro) {
      await api(`/api/macros/${delMacro.dataset.delMacro}`, { method: "DELETE" });
      lastMacroListKey = "";
      toast("Đã xoá macro");
      await refresh();
      return;
    }
    if (delItem) {
      await api(`/api/items/${delItem.dataset.delItem}`, { method: "DELETE" });
      toast("Đã xoá item");
      await refresh();
      return;
    }
    if (delLib) {
      await api(`/api/library/${delLib.dataset.delLib}`, { method: "DELETE" });
      toast("Đã xoá khỏi thư viện");
      await refresh();
      return;
    }
    if (libFilter) {
      libraryFilter = libFilter.dataset.libFilter;
      renderLibrary((lastState && lastState.library) || []);
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
      return;
    }
    if (delCrop) {
      await api(`/api/crops/${delCrop.dataset.delCrop}`, { method: "DELETE" });
      toast("Đã xoá cây trồng");
      await refresh();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

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
  pickerSlot = null;
}

function renderPickerGrid() {
  if (!pickerSlot) return;
  const rows = ((lastState && lastState.library) || []).filter((row) => row.kind === pickerSlot.kind);
  $("library-picker-grid").innerHTML = rows.map((row) => libraryCard(row, { pick: true })).join("");
  $("library-picker-empty").hidden = rows.length > 0;
}

async function showLibraryPicker(key) {
  pickerSlot = parsePickerKey(key);
  if (!pickerSlot) return;
  $("library-picker-file").value = "";
  $("library-picker-title").textContent =
    pickerSlot.kind === "news" ? "Chọn ảnh báo từ thư viện" : "Chọn ảnh shop từ thư viện";
  if (!lastState.library) lastState = await api("/api/state");
  renderPickerGrid();
  $("library-picker").showModal();
}

$("library-picker-cancel").addEventListener("click", () => {
  $("library-picker").close();
  pickerSlot = null;
});
$("library-picker").addEventListener("click", (ev) => {
  if (ev.target === $("library-picker")) {
    $("library-picker").close();
    pickerSlot = null;
  }
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
    lastState = state;
    render(state);
    applyLibraryPick(state.saved.id);
    toast(state.saved.duplicate ? "Đã có icon giống, dùng lại" : "Đã thêm vào thư viện");
  } catch (err) {
    toast(err.message, true);
  }
});

$("item-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  $("item-shop-lib").value = "";
  if (!file) {
    $("item-preview-img").removeAttribute("src");
    $("item-preview-img").hidden = true;
    syncItemPreview();
    return;
  }
  $("item-preview-img").src = await fileToDataUrl(file);
  $("item-preview-img").hidden = false;
  syncItemPreview();
});

$("item-news-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  $("item-news-lib").value = "";
  const img = $("item-news-preview-img");
  if (!file) {
    img.hidden = true;
    img.removeAttribute("src");
    syncItemPreview();
    return;
  }
  img.src = await fileToDataUrl(file);
  img.hidden = false;
  syncItemPreview();
});

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
    await api("/api/items", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("item-form").reset();
    $("item-shop-lib").value = "";
    $("item-news-lib").value = "";
    $("item-preview").hidden = true;
    $("item-preview-img").hidden = true;
    $("item-news-preview-img").hidden = true;
    toast("Đã lưu item");
    await refresh();
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
    shop.src = `/api/templates/${tile.dataset.template}.png?t=${Date.now()}`;
    shop.hidden = false;
  } else {
    shop.removeAttribute("src");
    shop.hidden = true;
  }
  if (tile.dataset.hasNews) {
    news.src = `/api/templates/${tile.dataset.newsTemplate}.png?t=${Date.now()}`;
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

$("item-edit-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  $("item-edit-shop-lib").value = "";
  const img = $("item-edit-preview");
  if (!file) return;
  img.src = await fileToDataUrl(file);
  img.hidden = false;
});

$("item-edit-news-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  $("item-edit-news-lib").value = "";
  const img = $("item-edit-news-preview");
  if (!file) return;
  img.src = await fileToDataUrl(file);
  img.hidden = false;
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
    await api(`/api/items/${oldId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    $("item-modal").close();
    toast("Đã cập nhật vật phẩm");
    await refresh();
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
    await api("/api/crops", { method: "POST", body: JSON.stringify(body) });
    $("crop-form").reset();
    toast("Đã lưu cây trồng");
    await refresh();
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
        loop_rest_min: Number($("loop_rest_min").value),
        action_wait_s: Number($("action_wait_s").value),
        buy_wait_s: Number($("buy_wait_s").value),
        swipe_duration_ms: Number($("swipe_duration_ms").value),
        debug: $("debug").checked,
      }),
    });
    toast("Đã lưu cấu hình");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

document.body.addEventListener("change", async (ev) => {
  const wait = ev.target.closest("[data-wait-idx]");
  if (wait) {
    try {
      const state = await api(`/api/macros/record/steps/${wait.dataset.waitIdx}`, {
        method: "PATCH",
        body: JSON.stringify({ ms: Number(wait.value) || 0 }),
      });
      lastStepsKey = "";
      renderMacros(state);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const toggle = ev.target.closest("[data-toggle]");
  if (!toggle) return;
  try {
    await api(`/api/items/${toggle.dataset.toggle}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: toggle.checked }),
    });
  } catch (err) {
    toast(err.message, true);
    toggle.checked = !toggle.checked;
  }
});

refresh()
  .then((_) => schedulePoll(false))
  .catch((err) => toast(err.message, true));
