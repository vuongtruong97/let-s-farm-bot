const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  running: false,
};

function api(path, options = {}) {
  return fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  });
}

function setPill(id, text, live) {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle("live", !!live);
  el.classList.toggle("idle", !live);
}

function applyState(payload) {
  if (!payload) return;
  state.config = payload.config;
  state.running = payload.running;
  $("adb-port").value = payload.config.adb_port;
  $("pause").value = payload.config.cycle_pause_s;
  $("grow").value = payload.config.demo_grow_s;
  setPill("device-pill", payload.device_name, payload.kind === "adb");
  setPill("run-pill", payload.running ? "Đang auto" : "Đang nghỉ", payload.running);
  document.querySelectorAll("#modes button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === payload.config.run_mode);
  });
  renderDetect(payload.stats);
  if (payload.stats && payload.stats.stats) renderStats(payload.stats);
  if (payload.logs) {
    $("log").innerHTML = "";
    payload.logs.forEach(pushLog);
  }
}

function renderStats(bundle) {
  const stats = bundle.stats || bundle;
  $("stat-ready").textContent = stats.ready ?? 0;
  $("stat-growing").textContent = stats.growing ?? 0;
  $("stat-empty").textContent = stats.empty ?? 0;
  $("stat-cycles").textContent = bundle.cycles ?? state.config?.cycles ?? $("stat-cycles").textContent;
}

function pushLog(item) {
  const li = document.createElement("li");
  if (item.level === "error") li.classList.add("error");
  li.innerHTML = `<span class="t">${item.t}</span>${item.message}`;
  const log = $("log");
  log.appendChild(li);
  log.scrollTop = log.scrollHeight;
}

function showFrame(b64) {
  const img = $("frame");
  img.src = `data:image/jpeg;base64,${b64}`;
  $("empty-frame").style.display = "none";
}

async function refreshShot() {
  const res = await fetch("/api/screenshot?overlay=true");
  const blob = await res.blob();
  $("frame").src = URL.createObjectURL(blob);
  $("empty-frame").style.display = "none";
}

async function saveConfig(patch) {
  const data = await api("/api/config", { method: "PUT", body: JSON.stringify(patch) });
  state.config = data.config;
}

function renderDetect(bundle) {
  if (!bundle) return;
  const rows = bundle.detected_rows ?? bundle.stats?.detected_rows;
  const cols = bundle.detected_cols ?? bundle.stats?.detected_cols;
  const plots = bundle.plot_count ?? bundle.stats?.plot_count;
  const seed = bundle.seed ?? bundle.stats?.seed;
  if (!plots) return;
  const seedText = seed ? ` · hạt giống (${seed.x},${seed.y})` : "";
  $("detect-note").textContent = `Tự thấy ${rows}×${cols} ô (${plots} thửa)${seedText}`;
}

$("btn-connect").addEventListener("click", async () => {
  const port = Number($("adb-port").value);
  const data = await api("/api/connect", {
    method: "POST",
    body: JSON.stringify({ port }),
  });
  if (!data.ok) {
    pushLog({ t: "now", level: "error", message: data.error });
  }
  applyState(data);
  await refreshShot();
});

$("btn-demo").addEventListener("click", async () => {
  applyState(await api("/api/demo", { method: "POST" }));
  await refreshShot();
});

$("btn-reset-demo").addEventListener("click", async () => {
  applyState(await api("/api/demo/reset", { method: "POST" }));
  await refreshShot();
});

$("btn-scan").addEventListener("click", async () => {
  const stats = await api("/api/scan", { method: "POST" });
  renderStats(stats);
  renderDetect(stats);
  await refreshShot();
});

$("btn-start").addEventListener("click", async () => {
  applyState(await api("/api/start", { method: "POST" }));
});
$("btn-stop").addEventListener("click", async () => {
  applyState(await api("/api/stop", { method: "POST" }));
});

["pause", "grow", "adb-port"].forEach((id) => {
  $(id).addEventListener("change", async () => {
    await saveConfig({
      cycle_pause_s: Number($("pause").value),
      demo_grow_s: Number($("grow").value),
      adb_port: Number($("adb-port").value),
    });
  });
});

document.querySelectorAll("#modes button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    await saveConfig({ run_mode: btn.dataset.mode });
    document.querySelectorAll("#modes button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "F8") {
    event.preventDefault();
    $("btn-start").click();
  }
  if (event.key === "F9") {
    event.preventDefault();
    $("btn-stop").click();
  }
});

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") applyState(msg);
    if (msg.type === "log") pushLog(msg);
    if (msg.type === "frame") {
      showFrame(msg.jpeg);
      renderStats(msg);
      renderDetect(msg);
      setPill("run-pill", msg.running ? "Đang auto" : "Đang nghỉ", msg.running);
      $("stat-cycles").textContent = msg.cycles ?? 0;
    }
  };
  ws.onclose = () => setTimeout(connectWs, 1200);
}

async function boot() {
  applyState(await api("/api/state"));
  await refreshShot();
  connectWs();
}

boot().catch((err) => {
  pushLog({ t: "--", level: "error", message: err.message });
});
