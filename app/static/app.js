const state = {
  config: {},
  candidates: [],
  current: null,
  style: "professional",
  generated: null,
  styleCopies: {}, // 5 种风格预生成结果 { style: {title, content, tags} }
  styleLoading: {},
  collageSelection: [],
  previewDebounce: null,
  excluded: new Set(), // 单张垃圾桶：本次发布排除
  editorAppPath: "",
  queue: [], // 任务队列条目
  queueIndex: 0,
  queueRunning: false,
  queueEnabled: false,
  scheduleSuggestion: "",
  activeProfile: "Default",
  viewedGroups: new Set(),
  logo: { enabled: false, path: "", scale: 18, opacity: 100, vpos: 92, hpos: 92 },
  watermark: { enabled: false, path: "", scale: 120, opacity: 40, tiled: true },
  saveOverlayLocal: false,
  latestUpdate: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, body = null) {
  const options = body
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    : {};
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    const msg = data.error || data.result?.stderr || data.result?.stdout || "请求失败";
    throw new Error(msg);
  }
  return data;
}

function log(message) {
  $("runLog").textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
}

function setBridge(status, ok = null) {
  const el = $("bridgeStatus");
  el.textContent = status;
  el.classList.toggle("ok", ok === true);
  el.classList.toggle("bad", ok === false);
}

function renderUpdateBanner(release) {
  const banner = $("updateBanner");
  state.latestUpdate = release;
  $("updateTitle").textContent = `发现新版本 ${release.version}`;
  $("updateNotes").textContent = release.notes || "包含软件功能和问题修复。";
  $("installUpdateBtn").disabled = !release.package_ready;
  $("installUpdateBtn").title = release.package_ready ? "下载更新并重启软件" : "此版本暂缺少完整更新包";
  banner.hidden = false;
}

async function checkForUpdates(silent = false) {
  const button = $("checkUpdatesBtn");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/update/check");
    if (data.update_available) {
      renderUpdateBanner(data.release);
      if (!silent) log(`发现新版本 ${data.latest_version}。`);
    } else {
      $("updateBanner").hidden = true;
      if (!silent) log(`当前已是最新版本 ${data.current_version}。`);
    }
  } catch (error) {
    if (!silent) log(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

async function waitForUpdatedServer(expectedVersion) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch("/api/version", { cache: "no-store" });
      if (response.ok) {
        const data = await response.json();
        if (data.version === expectedVersion) {
          location.reload();
          return;
        }
      }
    } catch (_) {
      // The service is expected to be unavailable while its process restarts.
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  log("更新程序仍在运行，但新版本服务尚未就绪。请查看 data/update.log。");
}

async function installUpdate() {
  const release = state.latestUpdate;
  if (!release?.package_ready) {
    log("当前 Release 缺少可验证的更新包。");
    return;
  }
  if (!confirm(`将下载版本 ${release.version} 并重启软件。当前未完成的发布任务不会丢失，但请确保队列已暂停。继续吗？`)) return;

  const button = $("installUpdateBtn");
  button.disabled = true;
  button.textContent = "正在启动更新…";
  try {
    const result = await api("/api/update/install", { version: release.version });
    $("updateTitle").textContent = `正在安装版本 ${result.version}`;
    $("updateNotes").textContent = "软件会在文件校验和重启完成后自动重新载入。";
    log(`已启动版本 ${result.version} 的更新，正在等待服务重启…`);
    await waitForUpdatedServer(result.version);
  } catch (error) {
    button.disabled = false;
    button.textContent = "下载并重启";
    log(error.message);
  }
}

function tagsFromInput() {
  return $("tagsInput").value
    .split(/[，,\s#]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 10);
}

function parseWindowLabel(label) {
  const times = [...String(label).matchAll(/([01]?\d|2[0-4]):([0-5]\d)/g)].map(
    (m) => `${m[1].padStart(2, "0")}:${m[2]}`,
  );
  if (!times.length) return null;
  const norm = (t) => (t === "24:00" ? "23:59" : t);
  if (times.length === 1) return [norm(times[0]), norm(times[0])];
  return [norm(times[0]), norm(times[1])];
}

function swRowHtml(range) {
  return `
    <div class="sw-row">
      <input type="time" class="sw-start" value="${range[0]}" />
      <span class="sw-sep">至</span>
      <input type="time" class="sw-end" value="${range[1]}" />
      <button type="button" class="sw-del ghost" title="删除该时间段">×</button>
    </div>
  `;
}

function renderScheduleEditor(windows = []) {
  const list = $("scheduleWindowList");
  list.innerHTML = windows.length
    ? windows.map((w) => swRowHtml(parseWindowLabel(w) || ["20:00", "22:00"])).join("")
    : swRowHtml(["20:00", "22:00"]);
  list.querySelectorAll(".sw-del").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rows = list.querySelectorAll(".sw-row");
      if (rows.length <= 1) return;
      btn.closest(".sw-row").remove();
    });
  });
}

function scheduleWindowsFromEditor() {
  return [...document.querySelectorAll("#scheduleWindowList .sw-row")]
    .map((row) => {
      const s = row.querySelector(".sw-start").value;
      const e = row.querySelector(".sw-end").value;
      if (!s || !e) return null;
      return s === e ? s : `${s}-${e}`;
    })
    .filter(Boolean);
}

function topicTemplatesFromInput() {
  return $("topicTemplates").value
    .split(/\n+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 50);
}

function refreshScheduleWindowOptions(windows = scheduleWindowsFromEditor()) {
  const current = $("scheduleWindow").value;
  $("scheduleWindow").innerHTML = windows.length
    ? windows.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("")
    : '<option value="">未设置时间段</option>';
  if (windows.includes(current)) {
    $("scheduleWindow").value = current;
  }
}

function refreshTopicTemplateOptions(templates = topicTemplatesFromInput()) {
  const current = $("topicTemplate").value;
  $("topicTemplate").innerHTML = templates.length
    ? templates.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("")
    : '<option value="">不使用模板</option>';
  if (templates.includes(current)) {
    $("topicTemplate").value = current;
  }
}

function currentPayload() {
  const current = state.current || {};
  const publishMode = $("publishMode").value;
  const allImages = current.selected_images || [];
  const images = allImages.filter((p) => !state.excluded.has(p));
  return {
    folder_path: current.path || "",
    folder_name: current.name || "",
    images,
    model_name: $("modelName").value.trim(),
    style: state.style,
    title: $("titleInput").value.trim(),
    content: $("contentInput").value.trim(),
    tags: tagsFromInput(),
    album: $("defaultAlbum").value.trim(),
    location: $("defaultLocation").value.trim(),
    topic_template: $("topicTemplate").value,
    shooting_date: $("shootingDate").value,
    shooting_location: $("defaultLocation").value.trim(),
    publish_mode: publishMode,
    schedule_window: publishMode === "scheduled" ? $("scheduleWindow").value : "",
    schedule_time: $("schedulePreview").value.replace(" ", "T"),
    profile_dir: state.activeProfile || "Default",
    original: true,
    visibility: $("visibility").value,
  };
}

function updateChecklist() {
  const payload = currentPayload();
  // 折叠态摘要：一眼看关键信息，无需展开
  const summary = $("checklistSummary");
  if (summary) {
    const mode = payload.publish_mode === "scheduled" ? "定时" : "立即";
    const time = payload.publish_mode === "scheduled" && payload.schedule_time
      ? ` ${payload.schedule_time.replace("T", " ")}`
      : "";
    summary.textContent = `${payload.images.length} 张图 · ${mode}${time} · 点开查看全部项`;
  }
  const items = [
    ["素材文件夹", payload.folder_path || "未选择"],
    ["图片数量", `${payload.images.length} 张`],
    ["标题", payload.title || "未生成"],
    ["正文", payload.content ? `${payload.content.length} 字` : "未生成"],
    ["话题", payload.tags.length ? payload.tags.map((t) => `#${t}`).join(" ") : "未生成"],
    ["话题模板", payload.topic_template || "未选择"],
    ["专辑", payload.album || "未设置"],
    ["拍摄地点", payload.shooting_location || "未设置"],
    ["拍摄日期", payload.shooting_date || "未识别"],
    ["发布方式", payload.publish_mode === "scheduled" ? "定时发布" : "立即发布"],
    ["时间段", payload.publish_mode === "scheduled" ? payload.schedule_window || "未选择" : "不使用"],
    ["原创声明", "自动勾选"],
    ["Logo/水印", fxParamsFromUI().fx_enabled ? (fxParamsFromUI().logo_enabled || fxParamsFromUI().watermark_enabled ? "启用，发布时自动叠加" : "启用（未选图，发原图）") : "关闭，发原图"],
    ["输出方式", { merge: "合并发布不动原图", suffix: "另存 -logo 后缀", replace: "替换原图（有备份）" }[fxParamsFromUI().output_mode] || "不动原图"],
    ["可见范围", payload.visibility],
    ["最终发布", "确认入队后由后台按账号串行提交"],
  ];
  $("checklist").innerHTML = items
    .map(([k, v]) => `<li><b>${escapeHtml(k)}</b><span>${escapeHtml(v)}</span></li>`)
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function imageUrl(path) {
  return `/api/image?path=${encodeURIComponent(path)}`;
}

function renderRecommendation(item) {
  state.current = item;
  state.excluded = new Set();
  state.styleCopies = {};
  $("modelName").value = item.model_name || "";
  $("shootingDate").value = item.shooting_date || "";
  $("recommendation").className = "recommend-card collapsed";
  $("recommendation").innerHTML = `
    <button id="recommendToggle" class="rc-toggle" type="button">
      <strong>${escapeHtml(item.display_name || item.relative_path || item.name)}</strong>
      <span class="rc-more">素材详情
        <svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
    </button>
    <div class="rc-detail">
      <div class="path">${escapeHtml(item.path)}</div>
      <div class="meta-row">
        <span class="chip">${item.image_count} 张图片</span>
        <span class="chip">层级 ${escapeHtml((item.hierarchy || []).join(" / ") || "根目录")}</span>
        <span class="chip">${item.year || item.month ? `归档 ${escapeHtml([item.year, item.month].filter(Boolean).join("-"))}` : "未识别年月"}</span>
        <span class="chip">最新 ${escapeHtml(item.latest_mtime_label)}</span>
        <span class="chip">拍摄日期 ${escapeHtml(item.shooting_date || "未识别")}</span>
        <span class="chip">${escapeHtml(item.score_reason)}</span>
      </div>
    </div>
  `;
  $("recommendToggle").addEventListener("click", () => {
    const card = $("recommendation");
    card.classList.toggle("collapsed");
  });
  const selected = item.selected_images || item.images || [];
  state.collageSelection = [];
  $("imageCountTotal").textContent = `共 ${selected.length} 张`;
  renderThumbStrip(selected);
  updateCoverPreview();
  updateChecklist();
  prefetchAllStyles();
}

// 推荐后自动生成全部 5 种风格文案：当前风格立即应用，其余后台静默生成
function prefetchAllStyles() {
  const all = ["praise", "professional", "process", "opinion", "story"];
  const initial = all.includes(state.style) ? state.style : "professional";
  log("正在自动生成 5 种风格文案，你可以先检查图片与封面…");
  document.querySelectorAll("[data-style]").forEach((b) => {
    b.classList.remove("is-ready");
    b.classList.add("is-loading");
  });
  const markReady = (style) => {
    document.querySelectorAll(`[data-style="${style}"]`).forEach((b) => {
      b.classList.remove("is-loading");
      b.classList.add("is-ready");
    });
  };
  let finished = 0;
  const afterOne = (style) => {
    markReady(style);
    finished += 1;
    if (finished === all.length) log("5 种风格文案已全部生成，点风格名即可切换。");
  };
  generateCopy(initial, { apply: true })
    .then(() => afterOne(initial))
    .catch((e) => log(`文案生成失败：${e.message}（可检查后台设置中的 AI Key）`));
  all
    .filter((s) => s !== initial)
    .forEach((s, i) => {
      setTimeout(() => {
        generateCopy(s, { apply: false })
          .then(() => afterOne(s))
          .catch(() => markReady(s));
      }, 500 * (i + 1));
    });
}

function renderThumbStrip(selected) {
  const addBtn = (pos) => `
    <button class="add-thumb" data-addpos="${pos}" type="button" title="手动指定图片/文件夹">+</button>`;
  const cells = selected
    .map(
      (path, i) => `
      <div class="thumb-cell${state.excluded.has(path) ? " is-excluded" : ""}" data-path="${escapeHtml(path)}" draggable="true" title="拖动可调整顺序">
        <span class="thumb-index">${i + 1}</span>
        <div class="thumb-actions">
          <button class="mini-icon-btn" data-edit-one="${escapeHtml(path)}" title="用编辑软件打开这一张">
            <svg viewBox="0 0 24 24"><path d="M4 17l-1 4 4-1L20 7l-3-3L4 17z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14 6l3 3" stroke="currentColor" stroke-width="1.6"/></svg>
          </button>
          <button class="mini-icon-btn" data-remove="${escapeHtml(path)}" title="从列表移除这张">
            <svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
        <img src="${imageUrl(path)}" alt="" loading="lazy" data-preview="${escapeHtml(path)}" />
      </div>
    `,
    )
    .join("");
  $("thumbGrid").className = "thumb-strip";
  $("thumbGrid").innerHTML = addBtn("before") + cells + addBtn("after");
  bindThumbStripEvents();
}

let dragSrcPath = null;

// 从列表直接移除一张图：增量更新 DOM（只删那一格 + 重新编号），不整条重渲染、不闪白
function removeImageFromList(path) {
  const list = state.current?.selected_images;
  if (!list) return;
  const idx = list.indexOf(path);
  if (idx < 0) return;
  const wasFirst = idx === 0;
  list.splice(idx, 1);
  const cell = $("thumbGrid").querySelector(`.thumb-cell[data-path="${CSS.escape(path)}"]`);
  if (cell) cell.remove(); // 只移除这一格，其他图片元素原封不动
  $("thumbGrid").querySelectorAll(".thumb-cell").forEach((el, i) => {
    el.querySelector(".thumb-index").textContent = i + 1;
  });
  $("imageCountTotal").textContent = `共 ${list.length} 张`;
  updateCoverPreview(); // 删的是第一张时自动切到新的第一张
  updateChecklist();
  log(`已从列表移除 ${path.split("/").pop()}（原图保留在素材文件夹），剩余 ${list.length} 张。`);
}

function reorderImages(srcPath, targetPath) {
  const list = state.current.selected_images;
  const from = list.indexOf(srcPath);
  const to = list.indexOf(targetPath);
  if (from < 0 || to < 0 || from === to) return;
  list.splice(from, 1);
  list.splice(to, 0, srcPath);
  renderThumbStrip(list);
  updateCoverPreview();
  updateChecklist();
  log("图片顺序已调整，封面已更新为当前第一张。");
}

function bindThumbStripEvents() {
  $("thumbGrid").querySelectorAll("[data-preview]").forEach((img) => {
    img.addEventListener("click", () => openPreview(img.dataset.preview));
  });
  $("thumbGrid").querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", () => removeImageFromList(btn.dataset.remove));
  });
  $("thumbGrid").querySelectorAll("[data-edit-one]").forEach((btn) => {
    btn.addEventListener("click", () => openInEditor([btn.dataset.editOne]));
  });
  $("thumbGrid").querySelectorAll("[data-addpos]").forEach((btn) => {
    btn.addEventListener("click", () => pickImagesManual());
  });

  // 拖拽排序（同小红书逻辑：拖到目标位置松手即重排）
  // 注意：不设 effectAllowed（默认 all），否则与拼图区 dropEffect 不兼容会导致 drop 被浏览器禁止
  $("thumbGrid").querySelectorAll(".thumb-cell").forEach((cell) => {
    cell.addEventListener("dragstart", (e) => {
      dragSrcPath = cell.dataset.path;
      cell.classList.add("dragging");
      try { e.dataTransfer.setData("text/plain", dragSrcPath); } catch (err) {}
    });
    cell.addEventListener("dragend", () => {
      cell.classList.remove("dragging");
      document.querySelectorAll(".drop-before, .drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
    });
    cell.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });
    cell.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const src = dragSrcPath || e.dataTransfer.getData("text/plain");
      if (src && src !== cell.dataset.path) reorderImages(src, cell.dataset.path);
    });
  });
}

// ---------- 图片区操作 ----------

async function ensureCandidates() {
  if (!state.candidates.length) {
    const data = await api("/api/scan", { root_path: $("rootPath").value.trim() });
    state.candidates = data.items;
    $("candidateCount").textContent = `${data.items.length} 个可用文件夹`;
  }
}

async function changeGroup() {
  await saveConfig();
  await ensureCandidates();
  if (state.current?.path) state.viewedGroups.add(state.current.path);
  const next = state.candidates.find((c) => !state.viewedGroups.has(c.path));
  if (!next) {
    log("所有素材组都看过了。可点 + 手动指定文件夹。");
    return;
  }
  state.styleCopies = {}; // 换组后文案缓存失效
  renderRecommendation({ ...next, selected_images: next.images.slice(0, 9).sort() });
  state.viewedGroups.add(next.path);
  log(`已换组：${next.name}`);
}

function clearImages() {
  if (!window.confirm("确定清空全部图片吗？\n原图文件保留，仅清空本次编辑区。")) return;
  state.current = null;
  state.excluded = new Set();
  state.styleCopies = {};
  $("thumbGrid").innerHTML = "";
  $("imageCountTotal").textContent = "共 0 张";
  $("recommendation").className = "empty recommend-empty";
  $("recommendation").textContent = "已清空。点「换一组」自动推荐，或点 + 手动指定。";
  updateCoverPreview(null);
  updateChecklist();
}

function refreshThumbs(showLog = false) {
  if (!state.current) return;
  renderThumbStrip(state.current.selected_images || []);
  updateCoverPreview();
  if (showLog) log("缩略图已刷新，已加载磁盘最新内容。");
}

async function openInEditor(paths) {
  if (!paths.length) { log("没有可编辑的图片。"); return; }
  // 未设置编辑软件时，先强制引导选择，取消则中止
  if (!state.editorAppPath) {
    log("首次使用，请先选择编辑图片的软件（如 Photoshop）。");
    const chosen = await pickEditorPath(true);
    if (!chosen) { log("未选择编辑软件，已取消。"); return; }
  }
  try {
    await api("/api/open-editor", { paths, app_path: state.editorAppPath });
    log(`已打开 ${paths.length} 张图片，在软件里保存后（直接替换原图），点刷新按钮查看。`);
  } catch (e) {
    log(e.message);
  }
}

async function pickEditorPath(silent = false) {
  try {
    const data = await api("/api/choose-app", {});
    state.editorAppPath = data.path || "";
    updateEditorPathUI();
    if (!silent && state.editorAppPath) log(`编辑软件：${state.editorAppPath}`);
    return !!state.editorAppPath;
  } catch (e) {
    if (!silent) log(e.message);
    return false;
  }
}

function updateEditorPathUI() {
  const name = state.editorAppPath ? state.editorAppPath.replace(/\/+$/, "").split("/").pop() : "";
  const nameEl = $("editorPathName");
  if (nameEl) nameEl.textContent = state.editorAppPath ? name : "未设置，点击选择";
  $("editorAppPath").value = state.editorAppPath;
  $("editorPathBtn").title = state.editorAppPath || "点击选择编辑软件";
  $("editorPathBtn").classList.toggle("is-set", !!state.editorAppPath);
}

async function pickImagesManual() {
  try {
    const data = await api("/api/choose-folder", {});
    if (!data.path) return;
    const folder = state.candidates.find((c) => c.path === data.path);
    if (folder) {
      state.styleCopies = {};
      renderRecommendation({ ...folder, selected_images: folder.images.slice(0, 9).sort() });
      state.viewedGroups.add(folder.path);
    } else {
      // 手动指定的文件夹不在扫描列表里，重新扫描后渲染
      const scanData = await api("/api/scan", { root_path: data.path });
      if (scanData.items[0]) {
        const item = scanData.items[0];
        state.styleCopies = {};
        renderRecommendation({ ...item, selected_images: item.images.slice(0, 9).sort() });
      }
    }
    log(`已手动指定：${data.path}`);
  } catch (e) {
    log(`文件夹选择接口阶段2接入：${e.message}`);
  }
}

// ---------- 右侧封面预览 ----------

function updateCoverPreview(path = null) {
  const img = $("coverPreviewImg");
  const ph = $("previewPlaceholder");
  // 拼图/水印/LOGO 模式下大预览区归各编辑器，封面图不抢占（否则刷新时布局错乱）
  if (activeRightTab !== "cover") {
    img.style.display = "none";
    ph.style.display = "none";
    return;
  }
  let cover = path;
  if (!cover && state.current) {
    const isCollageOutput = (p) => /[/\\](四宫格_|九宫格_)/.test(p);
    const pool = (state.current.selected_images || []).filter((p) => !state.excluded.has(p));
    cover = pool.find((p) => !isCollageOutput(p)) || pool[0] || "";
  }
  if (cover) {
    img.onload = () => applyExplicitSize(img, 600); // 显示比例=原图比例
    img.src = imageUrl(cover); // 不带时间戳，浏览器缓存直接命中不闪白
    img.style.display = "";
    ph.style.display = "none";
  } else {
    img.removeAttribute("src");
    img.style.display = "none";
    ph.style.display = "";
  }
}

// ---------- 任务队列 ----------

function renderQueue() {
  const list = $("queueList");
  const statusClass = (status) => ({ submitted: "done", submitting: "running", reviewed_page: "running", unknown: "unknown", failed: "failed" }[status] || "ready");
  const statusText = (status) => ({ submitted: "已提交", submitting: "提交中", reviewed_page: "页面已填入", unknown: "状态不明", failed: "失败" }[status] || "待提交");
  list.innerHTML = state.queue.length
    ? state.queue
        .map(
          (q, i) =>
            `<span class="queue-num ${statusClass(q.status)}" data-qidx="${i}" title="点击回看第 ${i + 1} 条（${statusText(q.status)}）">${i + 1}</span>`,
        )
        .join("")
    : `<span class="queue-num current">1</span>`;
  list.querySelectorAll("[data-qidx]").forEach((el) => {
    el.addEventListener("click", () => loadQueueItem(Number(el.dataset.qidx)));
  });
  const pending = state.queue.filter((q) => q.status === "queued").length;
  $("readyCount").textContent = pending;
  $("startQueueBtn").style.display = pending ? "" : "none";
  $("pauseQueueBtn").style.display = state.queueEnabled ? "" : "none";
  $("startQueueBtn").disabled = state.queueEnabled;
  updatePlanBar();
}

async function loadQueue() {
  const data = await api("/api/queue");
  state.queue = data.items || [];
  state.queueEnabled = !!data.enabled;
  renderQueue();
}

// 队列串行执行：逐条填表 → 拟人停留 → 软件模拟人工点击「定时发布」→ 随机间隔 → 下一条
async function runQueue() {
  const pending = state.queue.filter((q) => q.status === "queued");
  if (!pending.length) {
    log("队列里没有待执行的任务。");
    return;
  }
  try {
    await api("/api/queue/start", {});
    state.queueEnabled = true;
    renderQueue();
    log(`已启动后台提交：${pending.length} 条任务。即使刷新页面，队列仍保存在 SQLite 中。遇到登录、网络或页面异常会自动暂停。`);
    await loadQueue();
  } catch (e) {
    log(`启动队列失败：${e.message}`);
    throw e;
  }
}

async function pauseQueue() {
  await api("/api/queue/pause", {});
  state.queueEnabled = false;
  renderQueue();
  log("后台队列已暂停，已提交任务不会撤回；待提交任务保留在数据库中。 ");
}

async function enqueueCurrent() {
  const payload = currentPayload();
  if (!payload.images.length) throw new Error("没有可发布图片");
  if (!payload.title || !payload.content) throw new Error("请先生成或填写标题、正文");
  if (payload.publish_mode === "scheduled") {
    if (!payload.schedule_window) throw new Error("请先选择时间段");
    if (!payload.schedule_time) await previewSchedule();
  }
  const input = $("dialogTimeInput");
  const preview = $("schedulePreview").value;
  if (payload.publish_mode === "scheduled" && preview) {
    input.value = preview.replace(" ", "T");
  } else {
    const d = new Date(Date.now() + 60 * 60 * 1000);
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    input.value = d.toISOString().slice(0, 16);
  }
  $("scheduleDialog").style.display = "flex";
}

async function confirmEnqueue() {
  const time = $("dialogTimeInput").value;
  if (!time) throw new Error("请选择发布时间");
  const normalizedTime = time.replace(" ", "T").slice(0, 16);
  const payload = {
    ...currentPayload(),
    schedule_time: normalizedTime,
    // 自动建议时间发生冲突时允许后端顺延到下一个可用日；手动改过则严格校验。
    schedule_auto: !state.scheduleSuggestion || normalizedTime === state.scheduleSuggestion,
  };
  const data = await api("/api/queue/enqueue", { payload, schedule_time: normalizedTime });
  state.queue = [...state.queue.filter((q) => q.id !== data.job.id), data.job];
  state.scheduleSuggestion = data.job.schedule_time || "";
  $("scheduleDialog").style.display = "none";
  renderQueue();
  log(`第 ${state.queue.length} 条已入队，发布时间 ${(data.job.schedule_time || normalizedTime).replace("T", " ")}。正在加载下一组…`);
  resetEditorForm();
  const changed = await changeGroup().catch(() => null);
  if (!changed && !state.current) {
    log("队列已保存。没有更多可推荐素材时，可点 + 手动指定后继续入队。");
  }
}

function resetEditorForm() {
  ["titleInput", "contentInput", "tagsInput", "modelName", "notes"].forEach((id) => ($(id).value = ""));
  $("contentCount").textContent = "0";
  $("tagPreview").innerHTML = "";
  $("schedulePreview").value = "";
  state.scheduleSuggestion = "";
}

function loadQueueItem(idx) {
  const q = state.queue[idx];
  if (!q) return;
  const p = q.payload;
  showPage("publish");
  state.current = { path: p.folder_path, name: p.folder_name, selected_images: p.images };
  state.excluded = new Set();
  renderThumbStrip(p.images);
  $("titleInput").value = p.title;
  $("contentInput").value = p.content;
  $("contentCount").textContent = p.content.length;
  $("tagsInput").value = p.tags.map((t) => `#${t}`).join("");
  $("modelName").value = p.model_name;
  $("shootingDate").value = p.shooting_date;
  $("defaultAlbum").value = p.album;
  $("defaultLocation").value = p.location;
  $("schedulePreview").value = q.schedule_time.replace("T", " ");
  state.scheduleSuggestion = "";
  updateCoverPreview();
  updateChecklist();
  log(`已调回队列第 ${idx + 1} 条预览。已提交或提交中的任务不能通过这里修改。`);
}

async function loadConfig() {
  const data = await api("/api/config");
  state.config = data.config;
  state.activeProfile = data.config.active_profile || "Default";
  $("rootPath").value = data.config.root_path || "";
  $("defaultAlbum").value = data.config.default_album || "";
  $("defaultLocation").value = data.config.default_location || "";
  renderScheduleEditor(data.config.schedule_windows || []);
  refreshScheduleWindowOptions(data.config.schedule_windows || []);
  $("topicTemplates").value = (data.config.topic_templates || []).join("\n");
  refreshTopicTemplateOptions(data.config.topic_templates || []);
  $("dailyLimit").value = data.config.daily_limit || 3;
  $("visibility").value = data.config.visibility || "公开可见";
  $("copyEngineSelect").value = data.config.copy_engine || "auto";
  $("deepseekModel").value = data.config.deepseek_model || "deepseek-v4-pro";
  $("deepseekKey").placeholder = data.config.has_deepseek_key ? "已保存，可留空" : "填入 DeepSeek API Key";
  $("momentsTag").value = data.config.moments_tag || "";
  state.editorAppPath = data.config.editor_app_path || "";
  updateEditorPathUI();
  setBridge(`今日 ${data.config.today_usage_count || 0}/${data.config.daily_limit || 3}`, null);
  updateChecklist();
}

async function saveConfig() {
  const payload = {
    root_path: $("rootPath").value.trim(),
    default_album: $("defaultAlbum").value.trim(),
    default_location: $("defaultLocation").value.trim(),
    schedule_windows: scheduleWindowsFromEditor(),
    topic_templates: topicTemplatesFromInput(),
    daily_limit: Number($("dailyLimit").value || 3),
    visibility: $("visibility").value,
    copy_engine: $("copyEngineSelect").value,
    deepseek_model: $("deepseekModel").value,
    moments_tag: $("momentsTag").value.trim(),
  };
  if ($("deepseekKey").value.trim()) {
    payload.deepseek_api_key = $("deepseekKey").value.trim();
  }
  await api("/api/config", payload);
  refreshScheduleWindowOptions(payload.schedule_windows);
  refreshTopicTemplateOptions(payload.topic_templates);
  log("设置已保存。");
}

async function previewSchedule() {
  if ($("publishMode").value !== "scheduled") {
    $("schedulePreview").value = "立即发布";
    state.scheduleSuggestion = "";
    return;
  }
  const windowValue = $("scheduleWindow").value;
  if (!windowValue) {
    $("schedulePreview").value = "";
    state.scheduleSuggestion = "";
    return;
  }
  const data = await api("/api/schedule/preview", {
    schedule_window: windowValue,
    profile_dir: state.activeProfile || "Default",
  });
  state.scheduleSuggestion = data.schedule_time;
  $("schedulePreview").value = data.schedule_time.replace("T", " ");
}

async function scan() {
  await saveConfig();
  log("正在扫描素材文件夹...");
  const data = await api("/api/scan", { root_path: $("rootPath").value.trim() });
  state.candidates = data.items;
  $("candidateCount").textContent = `${data.items.length} 个可用文件夹`;
  if (data.items[0]) {
    renderRecommendation({ ...data.items[0], selected_images: data.items[0].images.slice(0, 9).sort() });
  }
  log(`扫描完成，找到 ${data.items.length} 个包含图片的素材文件夹。`);
}

async function recommend() {
  await saveConfig();
  log("正在按未发布和最近加入规则推荐素材...");
  const data = await api("/api/recommend", { root_path: $("rootPath").value.trim(), max_images: 9 });
  renderRecommendation(data.item);
  state.viewedGroups.add(data.item.path);
  log(`已推荐：${data.item.name}`);
}

async function generateCopy(style = state.style, opts = { apply: true }) {
  if (!state.current) {
    throw new Error("请先推荐一组素材");
  }
  if (state.styleCopies[style]) {
    if (opts.apply) applyCopy(style);
    return state.styleCopies[style];
  }
  if (state.styleLoading[style]) throw new Error("该风格正在生成中");
  state.styleLoading[style] = true;
  log(`正在生成「${styleLabel(style)}」文案...`);
  try {
    const data = await api("/api/generate", {
      style,
      model_name: $("modelName").value.trim(),
      folder_name: state.current.name,
      notes: $("notes").value.trim(),
      album: $("defaultAlbum").value.trim(),
      location: $("defaultLocation").value.trim(),
      copy_engine: $("copyEngineSelect").value,
      deepseek_model: $("deepseekModel").value,
    });
    state.generated = data.copy;
    state.styleCopies[style] = { ...data.copy, _style: style };
    if (opts.apply) applyCopy(style);
    log(`文案已生成：${data.copy.style_label}`);
    return data.copy;
  } finally {
    delete state.styleLoading[style];
  }
}

function styleLabel(style) {
  return { praise: "夸赞型", professional: "专业展示", process: "过程分享", opinion: "观点讨论", story: "案例故事" }[style] || style;
}

function applyCopy(style) {
  const copy = state.styleCopies[style];
  if (!copy) return;
  $("titleInput").value = copy.title;
  $("contentInput").value = copy.content;
  $("tagsInput").value = copy.tags.map((t) => `#${t}`).join(" ");
  $("copyEngine").textContent = copy.engine;
  $("contentCount").textContent = copy.content.length;
  const tp = $("tagPreview");
  if (tp) {
    tp.innerHTML = copy.tags.slice(0, 12).map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("");
  }
  state.style = style;
  document.querySelectorAll("[data-style]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.style === style);
  });
  updateChecklist();
}

async function selectStyle(style) {
  if (state.styleCopies[style]) {
    applyCopy(style);
    return;
  }
  await generateCopy(style);
}

async function checkLogin() {
  setBridge("检测中...");
  log("正在检测小红书登录状态...");
  const data = await api("/api/xhs/check-login", {});
  const result = data.result?.json || {};
  if (result.logged_in) {
    setBridge("小红书已登录", true);
    log("小红书登录状态正常。");
  } else {
    setBridge("需要扫码登录", false);
    log("未登录：请在小红书页面扫码登录后再试。");
  }
}

async function fillPublish() {
  const payload = currentPayload();
  if (!payload.images.length) throw new Error("没有可上传图片");
  if (!payload.title || !payload.content) throw new Error("请先生成或填写标题正文");
  const validation = await api("/api/validate", payload);
  if (validation.warnings?.length) {
    log(`检查提醒：\n${validation.warnings.map((w) => `- ${w}`).join("\n")}\n继续填入小红书发布页...`);
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  if (validation.schedule_time) {
    $("schedulePreview").value = validation.schedule_time.replace("T", " ");
  }
  log("正在填入小红书发布页，请不要操作浏览器窗口...");
  const data = await api("/api/xhs/fill-publish", payload);
  const manual = data.manual_fields?.note || "";
  if (data.manual_fields?.schedule_time) {
    $("schedulePreview").value = data.manual_fields.schedule_time.replace("T", " ");
  }
  const warnings = data.warnings?.length ? `\n提醒：${data.warnings.join("；")}` : "";
  log(`已填入小红书发布页，历史记录 #${data.history_id}。${warnings}\n${manual}\n请在小红书页面检查后手动点击发布。`);
  await refreshHistory();
}

function renderProfile(profile) {
  const box = $("profileBox");
  if (!profile) {
    box.className = "empty";
    box.textContent = "尚未生成。生成后文案会按账号定位/赛道/受众自动定制（约需 1-2 分钟）。";
    return;
  }
  box.className = "recommend-card";
  const topics = (profile.topics || []).map((t) => `#${t}`).join(" ");
  box.innerHTML = `
    <strong>${escapeHtml(profile.positioning || "")}</strong>
    <div class="meta-row" style="margin-top:6px">
      <span class="chip">${escapeHtml(profile.account_type || "")}</span>
      <span class="chip">赛道：${escapeHtml(profile.track || "")}</span>
      <span class="chip">${escapeHtml(profile.source_profile || "当前账号")}</span>
      <span class="chip">基于 ${profile.note_count || 0} 条笔记</span>
      <span class="chip">${escapeHtml(profile.generated_at || "")}</span>
    </div>
    <p style="margin:6px 0 0"><b>受众：</b>${escapeHtml(profile.audience || "")}</p>
    <p style="margin:4px 0 0"><b>风格：</b>${escapeHtml(profile.content_style || "")}</p>
    <p style="margin:4px 0 0"><b>规律：</b>${escapeHtml(profile.best_performing || "")}</p>
    <p style="margin:4px 0 0"><b>话题：</b>${escapeHtml(topics)}</p>
    <p style="margin:4px 0 0"><b>避免：</b>${escapeHtml(profile.avoid || "")}</p>
  `;
}

async function loadProfile() {
  const data = await api("/api/profile");
  state.activeProfile = data.profile_dir || state.activeProfile;
  if (data.profile) {
    $("profileKeywords").value = data.profile.manual_keywords || "";
    $("profileTopics").value = data.profile.manual_topics || "";
    $("profileNotes").value = data.profile.manual_notes || "";
  }
  renderProfile(data.profile);
}

async function generateProfile() {
  const btn = $("genProfileBtn");
  btn.disabled = true;
  btn.textContent = "生成中（约1-2分钟）…";
  $("profileBox").className = "empty";
  $("profileBox").textContent = "正在读取创作平台数据并生成画像，请勿操作浏览器…";
  log("正在生成账号画像，需要 1-2 分钟...");
  try {
    const data = await api("/api/profile", {
      max_count: 20,
      manual_keywords: $("profileKeywords").value.trim(),
      manual_topics: $("profileTopics").value.trim(),
      manual_notes: $("profileNotes").value.trim(),
    });
    renderProfile(data.profile);
    log("账号画像已生成，后续文案将按画像定制。");
  } finally {
    btn.disabled = false;
    btn.textContent = "一键生成画像";
  }
}

async function fillMoments() {
  const payload = currentPayload();
  if (!payload.images.length && !payload.content) {
    throw new Error("请先推荐素材并生成文案");
  }
  await saveConfig();
  const btn = $("momentsBtn");
  btn.disabled = true;
  btn.textContent = "正在填入朋友圈…";
  log("正在填入朋友圈（请勿操作电脑），完成后会停在发表按钮前...");
  try {
    const data = await api("/api/moments/publish", {
      images: payload.images.slice(0, 9),
      text: payload.content,
      tag: $("momentsTag").value.trim(),
      dryRun: true,
    });
    const steps = (data.result?.steps || []).join(" → ");
    log(`朋友圈已填好，停在发表前。${steps}\n请在微信中检查后手动点发表。`);
  } finally {
    btn.disabled = false;
    btn.textContent = "填入朋友圈（不点发表）";
  }
}

async function refreshHistory() {
  const data = await api("/api/history");
  $("historyList").innerHTML = data.items.length
    ? data.items
        .map(
          (item) => `
        <div class="history-item">
          <h3>${escapeHtml(item.title || item.folder_name)}</h3>
          <p>${escapeHtml(item.created_at)} · ${escapeHtml(item.status)} · ${escapeHtml(item.folder_name)}</p>
          <p>${escapeHtml(item.schedule_time ? `定时 ${item.schedule_time.replace("T", " ")} · ${item.schedule_window || ""}` : "立即/未定时")}</p>
          <p>${escapeHtml((item.tags || []).map((t) => `#${t}`).join(" "))}</p>
          <p>${escapeHtml((item.warnings || []).join("；"))}</p>
        </div>
      `,
        )
        .join("")
    : '<div class="empty">暂无历史。</div>';
}

// ---------- Logo / 水印 / 拼图 ----------

function fxParamsFromUI() {
  // 新设计：水印/LOGO 控件在右栏；此结构同时兼容旧合成接口
  return {
    fx_enabled: true,
    logo_enabled: state.logo.enabled,
    // 自由坐标（中心点百分比），后端 imagefx 支持 "v%,h%" 格式
    logo_position: `${state.logo.vpos ?? 92}%,${state.logo.hpos ?? 92}%`,
    logo_margin: 3,
    logo_scale: state.logo.scale,
    watermark_enabled: !!String(state.watermark.text || "").trim(),
    watermark_text: String(state.watermark.text || "").trim(),
    watermark_position: "center",
    watermark_opacity: state.watermark.opacity,
    watermark_scale: state.watermark.scale,
    output_mode: "merge",
  };
}

function fxQueryParams(path) {
  const p = fxParamsFromUI();
  const q = new URLSearchParams();
  q.set("path", path);
  Object.entries(p).forEach(([k, v]) => q.set(k, v));
  q.set("t", Date.now());
  return `/api/image/preview?${q.toString()}`;
}

function openPreview(path) {
  $("previewModalName").textContent = path.split("/").pop();
  $("previewModalImg").src = fxQueryParams(path);
  $("previewModal").style.display = "flex";
}

function closePreview() {
  $("previewModal").style.display = "none";
  $("previewModalImg").src = "";
}

async function saveFxSettings() {
  try {
    await api("/api/logo/settings", fxParamsFromUI());
  } catch (e) {
    log(`保存 Logo/水印设置失败：${e.message}`);
  }
}

let fxSaveTimer = null;
function saveFxSettingsDebounced() {
  clearTimeout(fxSaveTimer);
  fxSaveTimer = setTimeout(saveFxSettings, 900); // 输入停顿后自动持久化
}

function syncFxStateFromUI() {
  const num = (id) => { const el = $(id); return el ? Number(el.value) : null; };
  const ls = num("logoScale");
  const lo = num("logoOpacityNew");
  const lv = num("logoVPos");
  const lh = num("logoHPos");
  if (ls !== null) state.logo.scale = ls;
  if (lo !== null) state.logo.opacity = lo;
  if (lv !== null) state.logo.vpos = lv;
  if (lh !== null) state.logo.hpos = lh;
  const ws = num("watermarkScale");
  const wo = num("watermarkOpacity");
  if (ws !== null) state.watermark.scale = ws;
  if (wo !== null) state.watermark.opacity = wo;
  const wti = $("watermarkTextInput");
  if (wti) {
    state.watermark.text = wti.value;
    state.watermark.enabled = !!wti.value.trim();
  }
  const vals = [
    ["logoScaleVal", state.logo.scale + "%"],
    ["logoOpacityVal", state.logo.opacity + "%"],
    ["logoVPosVal", state.logo.vpos + "%"],
    ["logoHPosVal", state.logo.hpos + "%"],
    ["watermarkScaleVal", state.watermark.scale + "%"],
    ["watermarkOpacityVal", state.watermark.opacity + "%"],
  ];
  vals.forEach(([id, t]) => { const el = $(id); if (el) el.textContent = t; });
}

function scheduleFxUpdate() {
  syncFxStateFromUI();
  updateChecklist();
  updateFxPreview(); // 右栏水印/LOGO 预览实时联动
  clearTimeout(state.previewDebounce);
  state.previewDebounce = setTimeout(async () => {
    await saveFxSettings();
    const current = $("previewModalImg").src;
    const match = current.match(/path=([^&]+)/);
    if (match && $("previewModal").style.display !== "none") {
      $("previewModalImg").src = fxQueryParams(decodeURIComponent(match[1]));
    }
  }, 400);
}

function uploadOverlay(kind, input) {
  const file = input.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const data = await api("/api/logo/upload", { kind, data_base64: reader.result });
      if (kind === "logo") {
        state.logo.enabled = true;
        state.logo.path = data.path;
        const d = $("logoDrop");
        if (d) { d.classList.add("has-file"); d.querySelector("b").textContent = "已加载 LOGO：" + file.name; }
      } else {
        state.watermark.path = data.path;
      }
      await saveFxSettings();
      updateChecklist();
      updateFxPreview(); // 上传成功立即刷新右栏实时预览（LOGO/水印 tab）
      log(`${kind === "logo" ? "Logo" : "水印"}已上传：${data.path}`);
    } catch (e) {
      log(`上传失败：${e.message}`);
    }
  };
  reader.readAsDataURL(file);
}

async function loadFxSettings() {
  try {
    const data = await api("/api/logo/settings");
    const s = data.settings || {};
    const setVal = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
    if (s.logo_path) { state.logo.path = s.logo_path; state.logo.enabled = true; }
    if (s.watermark_text) {
      state.watermark.text = s.watermark_text;
      state.watermark.enabled = true;
      setVal("watermarkTextInput", s.watermark_text);
    } else if (s.watermark_path) {
      state.watermark.path = s.watermark_path; // 旧 PNG 水印仅保留路径，不再启用
    }
    state.logo.scale = s.logo_scale ?? 18;
    state.watermark.scale = s.watermark_scale ?? 120;
    state.watermark.opacity = s.watermark_opacity ?? 40;
    setVal("logoScale", state.logo.scale);
    setVal("watermarkScale", state.watermark.scale);
    setVal("watermarkOpacity", state.watermark.opacity);
    syncFxStateFromUI();
    if (state.logo.path) {
      const d = $("logoDrop"); if (d) { d.classList.add("has-file"); d.querySelector("b").textContent = "已加载 LOGO：" + state.logo.path.split("/").pop(); }
    }
  } catch (e) {
    /* 首次无设置，用默认值 */
  }
}

async function makeCollage() {
  // 新版拼图请使用右侧「封面拼图」功能区（拖拽/缩放/对调）。
  log("请使用右侧「封面拼图」功能，旧版拼图入口已下线。");
}

// ============================================================
// 拼图编辑器（2x2 / 3x3）
// ============================================================

const collage = {
  type: "2x2",
  cells: [], // 每项：{ path, scale, dx, dy }，空格为 null
  selected: -1,
};

const collageMeta = {}; // path -> {w, h} 原图尺寸

function collageN() {
  return collage.type === "3x3" ? 3 : 2;
}

function initCollage(type = "2x2") {
  collage.type = type;
  const n = collageN();
  collage.cells = Array.from({ length: n * n }, () => null);
  collage.selected = -1;
  document.querySelectorAll("[data-ctype]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.ctype === type);
  });
  renderCollage();
}

function cellHtml(cell, i) {
  if (!cell) {
    return `<div class="collage-cell empty" data-ccell="${i}"><span>拖图到这里</span></div>`;
  }
  return `
    <div class="collage-cell${collage.selected === i ? " selected" : ""}" data-ccell="${i}">
      <img src="${imageUrl(cell.path)}" alt="" draggable="false"
        style="transform: translate(${cell.dx}%, ${cell.dy}%) scale(${cell.scale})" />
    </div>`;
}

// 一键填入：按顺序填入图片列表前 n×n 张（跳过已排除），空格留白合成
function autoFillCollage() {
  if (!state.current) {
    log("请先推荐/选择一组素材。");
    return;
  }
  const n = collageN();
  const imgs = (state.current.selected_images || [])
    .filter((p) => !state.excluded.has(p))
    .slice(0, n * n);
  if (!imgs.length) {
    log("没有可用图片。");
    return;
  }
  for (let i = 0; i < n * n; i++) {
    collage.cells[i] = i < imgs.length ? { path: imgs[i], scale: 1, dx: 0, dy: 0 } : null;
    if (collage.cells[i]) loadImageMeta(imgs[i]).catch(() => {});
  }
  renderCollage();
  log(
    imgs.length < n * n
      ? `已一键填入前 ${imgs.length} 张，剩余 ${n * n - imgs.length} 格留空（合成时白底填充）。`
      : `已一键填入前 ${imgs.length} 张，拖动对调或滚轮缩放微调即可。`,
  );
}

function renderCollage() {
  const editor = $("collageEditor");
  const n = collageN();
  editor.className = "collage-editor";
  editor.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
  editor.style.gridTemplateRows = `repeat(${n}, 1fr)`;
  editor.innerHTML = collage.cells.map((cell, i) => cellHtml(cell, i)).join("");
  bindCollageCells();
}

// 增量更新单个格子（放入/替换/删除/对调），避免整宫格重建导致图片重载闪烁
function renderCell(idx) {
  const editor = $("collageEditor");
  const old = editor.querySelector(`[data-ccell="${idx}"]`);
  if (!old) {
    renderCollage();
    return;
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = cellHtml(collage.cells[idx], idx);
  const fresh = wrap.firstElementChild;
  old.replaceWith(fresh);
  bindCollageCells();
}

function loadImageMeta(path) {
  if (collageMeta[path]) return Promise.resolve(collageMeta[path]);
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => {
      collageMeta[path] = { w: im.naturalWidth, h: im.naturalHeight };
      resolve(collageMeta[path]);
    };
    im.onerror = () => reject(new Error("图片加载失败"));
    im.src = imageUrl(path);
  });
}

// cover 后图片在格内的基础宽高（相对格子宽高比）
async function coverBase(path) {
  const m = await loadImageMeta(path);
  const k = Math.max(1 / m.w, 1 / m.h); // 格子按 1x1 归一化
  return { bw: m.w * k, bh: m.h * k };
}

function clampPan(cellIdx) {
  const cell = collage.cells[cellIdx];
  const el = document.querySelector(`[data-ccell="${cellIdx}"]`);
  if (!cell || !el) return;
  const rect = el.getBoundingClientRect();
  if (!rect.width) return;
  // 异步拿 cover 尺寸：用缓存，没有则跳过 clamp
  const m = collageMeta[cell.path];
  if (!m) return;
  const k = Math.max(rect.width / m.w, rect.height / m.h);
  const bw = (m.w * k) / rect.width; // 相对格子宽
  const bh = (m.h * k) / rect.height;
  const xMax = ((bw * cell.scale - 1) / 2) * 100;
  const yMax = ((bh * cell.scale - 1) / 2) * 100;
  cell.dx = Math.max(-xMax, Math.min(xMax, cell.dx));
  cell.dy = Math.max(-yMax, Math.min(yMax, cell.dy));
}

// 只更新某格图片的 transform（缩放/平移零重建、零闪烁）
function applyCellTransform(idx) {
  const c = collage.cells[idx];
  const im = document.querySelector(`[data-ccell="${idx}"] img`);
  if (c && im) im.style.transform = `translate(${c.dx}%, ${c.dy}%) scale(${c.scale})`;
}

function bindCollageCells() {
  const editor = $("collageEditor");
  editor.querySelectorAll(".collage-cell").forEach((cellEl) => {
    if (cellEl.dataset.bound) return; // 增量更新时防止重复绑定事件
    cellEl.dataset.bound = "1";
    const idx = Number(cellEl.dataset.ccell);

    // 单击选中
    cellEl.addEventListener("click", () => {
      collage.selected = idx;
      document.querySelectorAll(".collage-cell").forEach((el) => el.classList.remove("selected"));
      cellEl.classList.add("selected");
    });

    // 从左栏拖入（HTML5 DnD）：dropEffect 必须与 dragstart 兼容，否则浏览器禁止 drop
    cellEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      cellEl.classList.add("drag-over");
    });
    cellEl.addEventListener("dragleave", () => cellEl.classList.remove("drag-over"));
    cellEl.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      cellEl.classList.remove("drag-over");
      const src = dragSrcPath || e.dataTransfer.getData("text/plain");
      if (!src) return;
      collage.cells[idx] = { path: src, scale: 1, dx: 0, dy: 0 };
      if (collage.selected < 0) collage.selected = idx;
      loadImageMeta(src).catch(() => {});
      renderCell(idx);
      log("图片已放入宫格。");
    });

    // 右键菜单：放大 / 缩小 / 删除此图 / 清空画布
    cellEl.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      openCollageMenu(e.clientX, e.clientY, idx);
    });

    // 滚轮缩放：只改 transform，不重建 DOM（丝滑无闪烁）
    cellEl.addEventListener(
      "wheel",
      (e) => {
        const c = collage.cells[idx];
        if (!c) return;
        e.preventDefault();
        collage.selected = idx;
        const next = Math.min(3, Math.max(1, c.scale * (e.deltaY < 0 ? 1.08 : 0.93)));
        c.scale = Math.round(next * 100) / 100;
        clampPan(idx);
        applyCellTransform(idx);
      },
      { passive: false },
    );

    // mousedown：格内平移 / 格间对调
    cellEl.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const c = collage.cells[idx];
      if (!c) return;
      e.preventDefault();
      collage.selected = idx;
      document.querySelectorAll(".collage-cell").forEach((el) => el.classList.remove("selected"));
      cellEl.classList.add("selected");

      const pan0 = { dx: c.dx, dy: c.dy };
      const rect0 = cellEl.getBoundingClientRect();
      let mode = "pending";
      let hoverIdx = -1;

      const cellAtPoint = (x, y) => {
        const el = document.elementFromPoint(x, y);
        const cc = el?.closest?.(".collage-cell");
        return cc ? Number(cc.dataset.ccell) : -1;
      };

      const onMove = (ev) => {
        if (mode === "pending") {
          const over = cellAtPoint(ev.clientX, ev.clientY);
          if (over !== idx) {
            mode = "swap";
          } else if (Math.abs(ev.clientX - e.clientX) + Math.abs(ev.clientY - e.clientY) > 3) {
            mode = "pan";
          }
        }
        if (mode === "pan") {
          // 1:1 手感：鼠标 1px = 图 1px（考虑 scale）
          c.dx = pan0.dx + ((ev.clientX - e.clientX) / rect0.width / c.scale) * 100;
          c.dy = pan0.dy + ((ev.clientY - e.clientY) / rect0.height / c.scale) * 100;
          clampPan(idx);
          const im = cellEl.querySelector("img");
          if (im) im.style.transform = `translate(${c.dx}%, ${c.dy}%) scale(${c.scale})`;
        } else if (mode === "swap") {
          const over = cellAtPoint(ev.clientX, ev.clientY);
          if (over !== hoverIdx) {
            document.querySelectorAll(".collage-cell").forEach((el) => el.classList.remove("swap-target"));
            hoverIdx = over;
            if (hoverIdx >= 0 && hoverIdx !== idx) {
              document.querySelector(`[data-ccell="${hoverIdx}"]`)?.classList.add("swap-target");
            }
          }
        }
      };

      const onUp = (ev) => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.querySelectorAll(".collage-cell").forEach((el) => el.classList.remove("swap-target"));
        if (mode === "swap") {
          const target = cellAtPoint(ev.clientX, ev.clientY);
          if (target >= 0 && target !== idx && collage.cells[target]) {
            // 对调图片及其缩放/位置（显式临时变量，避免引用歧义）
            const beforeA = collage.cells[idx];
            const beforeB = collage.cells[target];
            collage.cells[target] = beforeA;
            collage.cells[idx] = beforeB;
            const afterA = collage.cells[idx];
            const afterB = collage.cells[target];
            log(
              `对调 ${idx}⇄${target}：前 ${beforeA.path.split("/").pop()} / ${beforeB.path.split("/").pop()}` +
                ` → 后 ${afterA.path.split("/").pop()} / ${afterB.path.split("/").pop()}`,
            );
            renderCell(idx);
            renderCell(target);
          }
          // 未发生对调时无 DOM 变更，无需重绘
        }
        // pan 模式下样式已实时更新，无需重绘
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  });
}

// 拼图右键菜单
function closeCollageMenu() {
  $("collageCtxMenu")?.remove();
}

function openCollageMenu(x, y, idx) {
  closeCollageMenu();
  const c = collage.cells[idx];
  const menu = document.createElement("div");
  menu.id = "collageCtxMenu";
  menu.className = "collage-ctx-menu";
  menu.innerHTML = `
    <button data-act="zoomin" ${c ? "" : "disabled"}>放大</button>
    <button data-act="zoomout" ${c ? "" : "disabled"}>缩小</button>
    <button data-act="remove" ${c ? "" : "disabled"}>删除此图</button>
    <button data-act="clear">清空画布</button>
  `;
  document.body.appendChild(menu);
  // 防止溢出屏幕
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 8)}px`;

  menu.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    const cell = collage.cells[idx];
    if (act === "zoomin" && cell) {
      cell.scale = Math.min(3, Math.round(cell.scale * 1.15 * 100) / 100);
      clampPan(idx);
      applyCellTransform(idx);
    } else if (act === "zoomout" && cell) {
      cell.scale = Math.max(1, Math.round((cell.scale / 1.15) * 100) / 100);
      clampPan(idx);
      applyCellTransform(idx);
    } else if (act === "remove" && cell) {
      collage.cells[idx] = null;
      if (collage.selected === idx) collage.selected = -1;
      renderCell(idx);
      log(`已删除宫格 ${idx + 1} 的图片。`);
    } else if (act === "clear") {
      initCollage(collage.type);
      log("画布已清空。");
    }
    closeCollageMenu();
  });
}

// 任意点击 / Esc / 滚动关闭右键菜单
document.addEventListener("click", (e) => {
  if (!e.target.closest?.("#collageCtxMenu")) closeCollageMenu();
});
document.addEventListener("contextmenu", (e) => {
  if (!e.target.closest?.(".collage-cell")) closeCollageMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCollageMenu();
});

// 确认拼图：canvas 按编辑效果合成，保存到素材文件夹并插入封面
async function confirmCollage() {
  const n = collageN();
  const empty = collage.cells.filter(Boolean).length;
  if (!empty) {
    throw new Error("画布是空的，请先放入图片");
  }
  log(`正在合成拼图（${empty} 格有图${empty < n * n ? `，${n * n - empty} 格白底` : ""}）…`);
  const CELL_W = 750; // 每格 3:4（小红书封面竖版比例）
  const CELL_H = 1000;
  const canvas = document.createElement("canvas");
  canvas.width = n * CELL_W;
  canvas.height = n * CELL_H;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  for (let i = 0; i < collage.cells.length; i++) {
      const c = collage.cells[i];
      const col = i % n;
      const row = Math.floor(i / n);
      if (!c) continue; // 空格保留白底
      const im = new Image();
    await new Promise((res, rej) => {
      im.onload = res;
      im.onerror = () => rej(new Error("合成时图片加载失败"));
      im.src = imageUrl(c.path);
    });
    // cover 到 3:4 格
    const k = Math.max(CELL_W / im.naturalWidth, CELL_H / im.naturalHeight);
    const dw0 = im.naturalWidth * k;
    const dh0 = im.naturalHeight * k;
    const dw = dw0 * c.scale;
    const dh = dh0 * c.scale;
    const x = (CELL_W - dw) / 2 + (c.dx / 100) * CELL_W;
    const y = (CELL_H - dh) / 2 + (c.dy / 100) * CELL_H;
    ctx.save();
    ctx.beginPath();
    ctx.rect(col * CELL_W, row * CELL_H, CELL_W, CELL_H);
    ctx.clip();
    ctx.drawImage(im, col * CELL_W + x, row * CELL_H + y, dw, dh);
    ctx.restore();
  }

  const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.92));
  const dataUrl = await new Promise((res) => {
    const reader = new FileReader();
    reader.onload = () => res(reader.result);
    reader.readAsDataURL(blob);
  });

  const label = n === 2 ? "四宫格" : "九宫格";
  const d = new Date();
  const stamp =
    `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}` +
    `-${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}`;
  const filename = `${label}_${stamp}.jpg`;
  const saved = await api("/api/save-generated", {
    data_base64: dataUrl,
    folder: state.current.path,
    filename,
  });

  // 插入为第一张（封面）
  state.current.selected_images = [saved.path, ...state.current.selected_images.filter((p) => p !== saved.path)];
  renderThumbStrip(state.current.selected_images);
  switchRightTab("cover");
  updateCoverPreview(saved.path);
  updateChecklist();
  log(`拼图已确认：${saved.path}，已设为封面。可点类型按钮重新制作新拼图。`);
}

// ---------- 视图切换 / 导航 ----------

function showView(name) {
  ["publish", "records", "settings"].forEach((v) => {
    const el = $(`page${v[0].toUpperCase()}${v.slice(1)}`);
    if (el) el.style.display = v === name ? "" : "none";
  });
  const main = document.querySelector(".main-area");
  if (main) main.scrollTop = 0;
}

function handleNav(target) {
  if (target === "publish" || target === "settings" || target === "records") {
    showView(target);
  } else if (target === "follow") {
    log("「关注账号热度笔记」正在接入官方数据，当前版本先占位。");
  } else if (target === "hot") {
    log("「行业热点」正在接入官方数据，当前版本先占位。");
  }
}

// ---------- 三栏拖拽分栏 ----------

const PANE_KEY = "xhs_pane_widths_v1";
const paneLimits = {
  left: [150, 420],
  right: [260, 540],
};
let paneWidths = { left: 216, right: 320, collapsed: false };

function savePaneWidths() {
  try { localStorage.setItem(PANE_KEY, JSON.stringify(paneWidths)); } catch (e) {}
}

function applyPaneWidths() {
  const shell = $("appShell");
  const lw = paneWidths.collapsed ? 0 : paneWidths.left;
  shell.style.gridTemplateColumns = `${lw}px ${paneWidths.collapsed ? 0 : 6}px minmax(0,1fr) 6px ${paneWidths.right}px`;
  $("resizerLeft").style.display = paneWidths.collapsed ? "none" : "";
  $("sidebarExpand").style.display = paneWidths.collapsed ? "" : "none";
}

function initResizers() {
  try {
    const saved = JSON.parse(localStorage.getItem(PANE_KEY) || "null");
    if (saved && Number(saved.left) && Number(saved.right)) paneWidths = saved;
  } catch (e) {}
  applyPaneWidths();

  document.querySelectorAll(".resizer").forEach((handle) => {
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const which = handle.dataset.resize;
      const startX = e.clientX;
      const startW = paneWidths[which];
      const [min, max] = paneLimits[which];
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const onMove = (ev) => {
        const delta = ev.clientX - startX;
        const next = which === "left" ? startW + delta : startW - delta;
        paneWidths[which] = Math.min(max, Math.max(min, next));
        applyPaneWidths();
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        savePaneWidths();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  });

  // 左栏收起 / 展开
  $("sidebarToggle").addEventListener("click", () => {
    paneWidths.collapsed = true;
    applyPaneWidths();
    savePaneWidths();
  });
  $("sidebarExpand").addEventListener("click", () => {
    paneWidths.collapsed = false;
    applyPaneWidths();
    savePaneWidths();
  });
}

// ---------- 批量计划条联动 ----------

function updatePlanBar() {
  const total = Math.max(1, Number($("planTotal").value || 1));
  const perDay = Math.max(1, Number($("dailyLimit").value || 1));
  $("planTotal").value = total;
  $("dailyLimit").value = perDay;
  $("planDays").textContent = Math.ceil(total / perDay);
  $("planTotalView").textContent = total;
  $("planProgress").textContent = Math.min(state.queue.length, total);
}

function showMedia(kind) {
  const isImage = kind === "image";
  $("imagePanel").style.display = isImage ? "" : "none";
  $("videoPanel").style.display = isImage ? "none" : "";
  $("mediaTabImage").classList.toggle("active", isImage);
  $("mediaTabVideo").classList.toggle("active", !isImage);
}

let activeRightTab = "cover"; // 当前右栏 tab，防止预览图抢占拼图区

// ---------- Logo / 水印实时预览 ----------
// 底图=第一张图（保持真实宽高比），滑杆调整即时叠加，所见即所得
function firstUsableImage() {
  if (!state.current) return null;
  const isCollageOutput = (p) => /[/\\](四宫格_|九宫格_)/.test(p); // 历史拼图合成品不当预览底图
  return (
    (state.current.selected_images || []).find((p) => !state.excluded.has(p) && !isCollageOutput(p)) ||
    (state.current.selected_images || []).find((p) => !state.excluded.has(p)) ||
    null
  );
}

// 显式计算图片显示尺寸：显示比例严格等于原图比例。
// 尺寸有固定上限（宽 420 / 高 560）：右栏拉得再宽，预览图到上限就不再变大，
// 多余空间留白；拉窄时等比缩小。比例永远恒等于原图，绝不变形。
function applyExplicitSize(img, maxH = 560, maxWCAP = 420) {
  const fit = () => {
    const nw = img.naturalWidth;
    const nh = img.naturalHeight;
    if (!nw || !nh) return;
    const parent = img.closest(".right-preview") || img.parentElement;
    const maxW = Math.min(Math.max(160, ((parent && parent.clientWidth) || 420) - 14), maxWCAP);
    const k = Math.min(maxW / nw, maxH / nh); // 等比缩放，比例恒等于原图
    img.style.width = `${Math.round(nw * k)}px`;
    img.style.height = `${Math.round(nh * k)}px`;
    img.style.maxWidth = "none";
    img.style.maxHeight = "none";
  };
  fit();
  if (img.__fitRO) img.__fitRO.disconnect();
  const host = img.closest(".right-preview") || img.parentElement || document.body;
  const ro = new ResizeObserver(fit);
  ro.observe(host);
  img.__fitRO = ro;
}

function updateFxPreview() {
  const tab = activeRightTab;
  if (tab !== "watermark" && tab !== "logo") return;
  const box = $("fxPreviewBox");
  const base = $("fxBaseImg");
  const wmLayer = $("fxWatermarkLayer");
  const logoLayer = $("fxLogoLayer");
  const hint = $("fxHint");
  const first = firstUsableImage();
  if (!first) {
    box.style.display = "none";
    return;
  }
  box.style.display = "";
  box.style.aspectRatio = ""; // 图片自适应显示，不再强制容器比例
  if (base.dataset.src !== first) {
    base.dataset.src = first;
    base.onload = () => {
      applyExplicitSize(base); // 显式像素尺寸：显示比例=原图比例，不受任何样式影响
      log(`预览底图：${first.split("/").pop()}（${base.naturalWidth}×${base.naturalHeight}，比例 ${(base.naturalWidth / base.naturalHeight).toFixed(2)}）`);
      updateFxPreview();
    };
    base.src = `${imageUrl(first)}&t=${Date.now()}`;
  } else {
    applyExplicitSize(base);
  }
  const bw = base.naturalWidth || 3;
  const bh = base.naturalHeight || 4;
  const short = Math.min(bw, bh);

  if (tab === "watermark") {
    logoLayer.style.display = "none";
    const text = (state.watermark.text || "").trim();
    const on = state.watermark.enabled && !!text;
    wmLayer.style.display = on ? "" : "none";
    hint.textContent = on ? "" : "输入水印文字，实时生成斜45°交叉网格虚线水印";
    if (on) {
      drawGridWatermarkLayer(wmLayer, base, text, state.watermark.scale, state.watermark.opacity);
    }
  } else {
    wmLayer.style.display = "none";
    const on = state.logo.enabled && !!state.logo.path;
    logoLayer.style.display = on ? "" : "none";
    hint.textContent = on ? "" : "上传 LOGO PNG 后，这里按第一张图实时显示位置与大小";
    if (on) {
      const applySize = () => {
        // 后端规则：LOGO 短边 = 图短边 × scale%（保持 LOGO 原比例）
        const lw = logoLayer.naturalWidth || 1;
        const lh = logoLayer.naturalHeight || 1;
        const target = (short * state.logo.scale) / 100;
        const k = lw >= lh ? target / lh : target / lw;
        logoLayer.style.width = `${((lw * k) / bw) * 100}%`;
      };
      if (logoLayer.dataset.src !== state.logo.path) {
        logoLayer.dataset.src = state.logo.path;
        logoLayer.onload = () => { applySize(); };
        logoLayer.src = `${imageUrl(state.logo.path)}&t=${Date.now()}`;
      } else {
        applySize();
      }
      logoLayer.style.left = `${state.logo.hpos}%`;
      logoLayer.style.top = `${state.logo.vpos}%`;
      logoLayer.style.opacity = (state.logo.opacity ?? 100) / 100;
    }
  }
}

// ---------- 斜45°交叉网格虚线水印（前端预览绘制，参数映射与后端 imagefx 一致） ----------
function drawGridWatermarkLayer(wmLayer, base, text, scalePct, opacity) {
  const dispW = base.clientWidth || 400;
  const dispH = base.clientHeight || 533;
  const dpr = 2; // 2x 分辨率保证清晰
  const canvasKey = `${text}|${scalePct}|${opacity}|${dispW}x${dispH}`;
  if (wmLayer.dataset.key === canvasKey) return; // 参数未变不重绘
  wmLayer.dataset.key = canvasKey;
  const w = dispW * dpr;
  const h = dispH * dpr;
  const cv = document.createElement("canvas");
  cv.width = w;
  cv.height = h;
  const d = cv.getContext("2d");
  const short = Math.min(w, h);
  const cell = Math.max(60, short * (scalePct / 100) * 0.22); // 与后端一致
  const fontPx = Math.max(10, cell * 0.13);
  const alpha = Math.min(100, Math.max(0, opacity)) / 100;
  d.strokeStyle = `rgba(255,255,255,${alpha * 0.62})`; // 线比字略淡
  d.fillStyle = `rgba(255,255,255,${alpha})`;
  d.lineWidth = Math.max(1, cell * 0.004);
  const dash = cell * 0.045;
  const gap = cell * 0.032;
  // 在旋转 -45° 的坐标系里画“水平网格 + 文字踩线”，再整体转回来：
  // 文字基线=横线（真正踩在虚线上），文字中心=竖线，屏幕上即 45° 交叉网格
  d.save();
  d.translate(w / 2, h / 2);
  d.rotate(-Math.PI / 4);
  const L = Math.hypot(w, h) + cell * 2;
  const half = L / 2;
  const k0 = Math.ceil(-half / cell);
  const k1 = Math.floor(half / cell);
  for (let k = k0; k <= k1; k++) {
    const c = k * cell;
    for (let p = -half; p < half; p += dash + gap) { // 水平虚线
      d.beginPath();
      d.moveTo(p, c);
      d.lineTo(Math.min(p + dash, half), c);
      d.stroke();
    }
    for (let p = -half; p < half; p += dash + gap) { // 垂直虚线
      d.beginPath();
      d.moveTo(c, p);
      d.lineTo(c, Math.min(p + dash, half));
      d.stroke();
    }
  }
  d.font = `300 ${fontPx}px "Hiragino Sans GB", "PingFang SC", sans-serif`; // W3 细字重
  d.textAlign = "center";
  d.textBaseline = "alphabetic"; // y 参数即基线 → 精确踩线
  for (let i = k0; i <= k1; i++) {
    for (let j = k0; j <= k1; j++) {
      d.fillText(text, i * cell, j * cell);
    }
  }
  d.restore();
  wmLayer.style.backgroundSize = "100% 100%";
  wmLayer.style.opacity = "1";
  wmLayer.style.backgroundImage = `url("${cv.toDataURL("image/png")}")`;
}

function switchRightTab(tab) {
  activeRightTab = tab;
  document.querySelectorAll("[data-rtab]").forEach((b) => b.classList.toggle("active", b.dataset.rtab === tab));
  document.querySelectorAll(".right-controls").forEach((c) => {
    c.style.display = c.dataset.rc === tab ? "" : "none";
  });
  const imgEl = $("coverPreviewImg");
  const ph = $("previewPlaceholder");
  const collageEditor = $("collageEditor");
  const fxBox = $("fxPreviewBox");
  if (tab === "collage") {
    if (imgEl) imgEl.style.display = "none";
    if (ph) ph.style.display = "none";
    if (fxBox) fxBox.style.display = "none";
    if (collageEditor) collageEditor.style.display = "grid";
  } else if (tab === "watermark" || tab === "logo") {
    if (collageEditor) collageEditor.style.display = "none";
    if (imgEl) imgEl.style.display = "none";
    if (ph) ph.style.display = "none";
    updateFxPreview();
  } else {
    if (collageEditor) collageEditor.style.display = "none";
    if (fxBox) fxBox.style.display = "none";
    const hasCover = imgEl && imgEl.getAttribute("src");
    if (imgEl) imgEl.style.display = hasCover ? "" : "none";
    if (ph) ph.style.display = hasCover ? "none" : "";
  }
}

function bindEvents() {
  // 左栏导航（图标栏 + 侧栏统一委托）
  document.querySelectorAll("[data-nav]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      handleNav(el.dataset.nav);
    });
  });

  // 平台下拉
  $("platformBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const m = $("platformMenu");
    m.style.display = m.style.display === "none" ? "" : "none";
  });
  document.addEventListener("click", () => { $("platformMenu").style.display = "none"; });
  $("platformMenu").addEventListener("click", (e) => e.stopPropagation());

  // 媒体切换
  $("mediaTabImage").addEventListener("click", () => showMedia("image"));
  $("mediaTabVideo").addEventListener("click", () => showMedia("video"));

  // 批量计划条
  ["planTotal", "dailyLimit"].forEach((id) => {
    $(id).addEventListener("input", () => { updatePlanBar(); updateChecklist(); });
    $(id).addEventListener("change", () => { updatePlanBar(); saveConfig().catch(() => {}); });
  });

  // 右栏功能 tab
  document.querySelectorAll("[data-rtab]").forEach((b) => {
    b.addEventListener("click", () => switchRightTab(b.dataset.rtab));
  });

  // 拼图类型切换
  document.querySelectorAll("[data-ctype]").forEach((b) => {

    b.addEventListener("click", () => initCollage(b.dataset.ctype));
  });
  $("collageAutoFillBtn").addEventListener("click", autoFillCollage);

  // 配置 / 素材
  $("saveConfigBtn").addEventListener("click", () => saveConfig().catch((e) => log(e.message)));
  $("addWindowBtn").addEventListener("click", () => {
    const list = $("scheduleWindowList");
    list.insertAdjacentHTML("beforeend", swRowHtml(["20:00", "22:00"]));
    const rows = list.querySelectorAll(".sw-row");
    const row = rows[rows.length - 1];
    row.querySelector(".sw-del").addEventListener("click", () => {
      if (list.querySelectorAll(".sw-row").length <= 1) return;
      row.remove();
      refreshScheduleWindowOptions();
    });
  });
  $("recommendBtn").addEventListener("click", () => recommend().catch((e) => log(e.message)));
  $("checklistToggle").addEventListener("click", () => {
    $("checklistCard").classList.toggle("collapsed");
  });
  $("checkLoginBtn").addEventListener("click", () => checkLogin().catch((e) => {
    log(e.message);
  }));
  $("checkUpdatesBtn").addEventListener("click", () => checkForUpdates(false));
  $("installUpdateBtn").addEventListener("click", () => installUpdate());
  $("dismissUpdateBtn").addEventListener("click", () => { $("updateBanner").hidden = true; });

  // 图片编辑区按钮
  $("changeGroupBtn").addEventListener("click", () => changeGroup().catch((e) => log(e.message)));
  $("clearImagesBtn").addEventListener("click", () => clearImages());
  $("refreshImagesBtn").addEventListener("click", () => refreshThumbs(true));
  $("batchEditBtn").addEventListener("click", () => {
    const p = currentPayload();
    openInEditor(p.images);
  });
  $("editorPathBtn").addEventListener("click", () => pickEditorPath().catch((e) => log(e.message)));
  $("collapseImagesBtn").addEventListener("click", () => {
    $("thumbGrid").classList.toggle("collapsed");
  });

  // 文案：AI 生成 + 风格切换
  $("generateBtn")?.addEventListener("click", () => generateCopy().catch((e) => log(e.message)));
  document.querySelectorAll("[data-style]").forEach((btn) => {
    btn.addEventListener("click", () => selectStyle(btn.dataset.style).catch((e) => log(e.message)));
  });

  // 正文计数
  $("contentInput").addEventListener("input", () => {
    $("contentCount").textContent = $("contentInput").value.length;
  });

  // 底栏：立即发布 / 定时入队
  $("immediatePublishBtn").addEventListener("click", () => fillPublish().catch((e) => log(e.message)));
  $("schedulePublishBtn").addEventListener("click", () => enqueueCurrent().catch((e) => log(e.message)));
  $("nextQueueBtn").addEventListener("click", () => loadNextQueue().catch((e) => log(e.message)));
  $("startQueueBtn").addEventListener("click", () => runQueue().catch((e) => log(e.message)));
  $("pauseQueueBtn").addEventListener("click", () => pauseQueue().catch((e) => log(e.message)));

  // 定时弹窗
  $("dialogCancelBtn").addEventListener("click", () => { $("scheduleDialog").style.display = "none"; });
  $("dialogConfirmBtn").addEventListener("click", () => confirmEnqueue().catch((e) => log(e.message)));

  // 历史 / 画像
  $("refreshHistoryBtn").addEventListener("click", () => refreshHistory().catch((e) => log(e.message)));
  $("genProfileBtn").addEventListener("click", () => generateProfile().catch((e) => {
    renderProfile(null);
    log(e.message);
  }));
  $("momentsBtn").addEventListener("click", () => log("朋友圈同步即将支持。"));

  // 拼图（旧入口，阶段3替换为新编辑器）
  $("collageConfirmBtn").addEventListener("click", () => confirmCollage().catch((e) => log(e.message)));

  // 大图预览
  $("previewCloseBtn").addEventListener("click", closePreview);
  $("previewModal").addEventListener("click", (e) => {
    if (e.target === $("previewModal")) closePreview();
  });

  // 水印文字 / LOGO 上传
  $("logoFile").addEventListener("change", () => uploadOverlay("logo", $("logoFile")));
  const wti = $("watermarkTextInput");
  wti.addEventListener("input", () => { scheduleFxUpdate(); saveFxSettingsDebounced(); });
  ["logoScale", "logoOpacityNew", "logoVPos", "logoHPos", "watermarkOpacity", "watermarkScale"].forEach((id) => {
    $(id).addEventListener("input", scheduleFxUpdate);
    $(id).addEventListener("change", scheduleFxUpdate);
  });

  // LOGO 拖放区点击打开文件选择
  $("logoDrop").addEventListener("click", (e) => { if (e.target.tagName !== "INPUT") $("logoFile").click(); });
  {
    const d = $("logoDrop");
    d.addEventListener("dragover", (e) => { e.preventDefault(); d.classList.add("dragover"); });
    d.addEventListener("dragleave", () => d.classList.remove("dragover"));
    d.addEventListener("drop", (e) => {
      e.preventDefault();
      d.classList.remove("dragover");
      const f = $("logoFile");
      f.files = e.dataTransfer.files;
      f.dispatchEvent(new Event("change"));
    });
  }

  // radio 与 hidden select 同步
  document.querySelectorAll("input[name='visibilityRadio']").forEach((r) => {
    r.addEventListener("change", () => {
      if (r.checked) { $("visibility").value = r.value; updateChecklist(); }
    });
  });
  document.querySelectorAll("input[name='publishRadio']").forEach((r) => {
    r.addEventListener("change", () => {
      if (r.checked) {
        $("publishMode").value = r.value;
        previewSchedule().catch((e) => log(e.message));
      }
    });
  });

  // 时间段 / 模板编辑器联动
  $("scheduleWindowList").addEventListener("input", () => {
    refreshScheduleWindowOptions();
    updateChecklist();
  });
  $("topicTemplates").addEventListener("input", () => {
    refreshTopicTemplateOptions();
    updateChecklist();
  });
  $("scheduleWindow").addEventListener("change", () => previewSchedule().catch((e) => log(e.message)));
  $("publishMode").addEventListener("change", () => previewSchedule().catch((e) => log(e.message)));

  // 表单变更刷新清单
  [
    "titleInput", "contentInput", "tagsInput", "defaultAlbum", "defaultLocation",
    "topicTemplate", "shootingDate", "scheduleWindow", "publishMode",
    "copyEngineSelect", "deepseekModel", "modelName",
  ].forEach((id) => {
    $(id).addEventListener("input", updateChecklist);
    $(id).addEventListener("change", updateChecklist);
  });
}

async function init() {
  bindEvents();
  initResizers();
  initCollage("2x2");
  updatePlanBar();
  renderQueue();
  await loadConfig();
  await loadFxSettings();
  await loadProfile().catch(() => {});
  await previewSchedule().catch(() => {});
  await refreshHistory();
  await loadQueue().catch((e) => log(`读取后台队列失败：${e.message}`));
  initAccountBar();
  initCoverPanel();
  log(`工作台已就绪，当前版本 ${state.config.app_version || "未知"}。`);
  checkForUpdates(true);
}

init().catch((err) => log(err.message));

// ---------- 右上角多账号（Chrome Profile 隔离，每个账号独立记住登录态） ----------

function initAccountBar() {
  $("accountBtn").addEventListener("click", async () => {
    const menu = $("accountMenu");
    if (menu.style.display !== "none") {
      menu.style.display = "none";
      return;
    }
    menu.style.display = "";
    menu.innerHTML = '<div class="account-item muted">读取中…</div>';
    try {
      const data = await api("/api/accounts/list", {});
      renderAccountMenu(data);
    } catch (e) {
      menu.innerHTML = `<div class="account-item muted">${escapeHtml(e.message)}</div>`;
    }
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".account-bar")) $("accountMenu").style.display = "none";
  });
  refreshAccountLabel();
}

function renderAccountMenu(data) {
  const menu = $("accountMenu");
  const rows = (data.profiles || [])
    .map((p) => {
      const active = p.dir === data.active;
      return `
        <button class="account-item${active ? " is-active" : ""}" data-profile="${escapeHtml(p.dir)}" type="button">
          <span class="account-check">${active ? "✓" : ""}</span>
          <span class="account-label">${escapeHtml(p.label)}</span>
          <span class="account-dir">${escapeHtml(p.dir)}</span>
        </button>`;
    })
    .join("");
  menu.innerHTML = `
    <div class="account-head">Chrome 浏览器账号（每个独立记住登录态）</div>
    ${rows || '<div class="account-item muted">未检测到 Chrome 账号</div>'}
    <div class="account-sep"></div>
    <button class="account-item" id="accountLogoutBtn" type="button"><span class="account-check">⏻</span><span class="account-label">退出当前账号（清登录态）</span></button>
    <div class="account-tip">添加新账号：在 Chrome 里新建用户资料并登录小红书，然后重新打开此菜单。</div>
  `;
  menu.querySelectorAll("[data-profile]").forEach((btn) => {
    btn.addEventListener("click", () => switchAccount(btn.dataset.profile, btn.querySelector(".account-label").textContent));
  });
  const logout = $("accountLogoutBtn");
  if (logout) logout.addEventListener("click", logoutAccount);
}

async function refreshAccountLabel() {
  try {
    const data = await api("/api/accounts/list", {});
    state.activeProfile = data.active || state.activeProfile;
    const p = (data.profiles || []).find((x) => x.dir === data.active) || (data.profiles || [])[0];
    $("accountName").textContent = p ? p.label : "账号";
  } catch {
    /* 静默 */
  }
}

async function switchAccount(profileDir, label) {
  $("accountMenu").style.display = "none";
  setBridge("切换中：正在重启浏览器…", null);
  log(`正在切换到账号「${label}」：Chrome 将自动重启，约 10-30 秒…`);
  try {
    const data = await api("/api/accounts/switch", { profile: profileDir });
    if (data.ready) {
      setBridge(`已连接：${label}`, true);
      log(`已切换到「${label}」，浏览器记录（登录状态）已复用。`);
    } else {
      setBridge("扩展未连接", false);
      log(`已切到「${label}」，但该浏览器账号未连上扩展：请确认该 Chrome 用户资料已安装并启用 XHS Bridge 扩展。`);
    }
    $("accountName").textContent = label;
    state.activeProfile = profileDir;
    await loadProfile().catch(() => renderProfile(null));
  } catch (e) {
    setBridge("切换失败", false);
    log(`切换账号失败：${e.message}`);
  }
}

async function logoutAccount() {
  if (!confirm("确定退出当前账号？将清除这个浏览器账号的小红书登录状态（其他账号不受影响）。")) return;
  $("accountMenu").style.display = "none";
  try {
    await api("/api/accounts/logout", {});
    log("已退出当前账号并清除其登录状态。");
  } catch (e) {
    log(`退出失败：${e.message}`);
  }
}

// ---------- AI 封面（API易图生图，配置预填自样片工厂） ----------

function initCoverPanel() {
  loadCoverConfig();
  $("coverGenerateBtn").addEventListener("click", generateCover);
}

async function loadCoverConfig() {
  try {
    const cfg = await api("/api/cover/config", {});
    const modelSel = $("coverModel");
    modelSel.innerHTML = cfg.models
      .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`)
      .join("");
    modelSel.value = cfg.default_model || cfg.models[0]?.id || "";
    const model = cfg.models.find((m) => m.id === modelSel.value);
    if (model) $("coverResolution").value = model.default_resolution;
    modelSel.addEventListener("change", () => {
      const m = cfg.models.find((x) => x.id === modelSel.value);
      if (m) $("coverResolution").innerHTML = m.resolutions
        .map((r) => `<option value="${r}"${r === m.default_resolution ? " selected" : ""}>${r.toUpperCase()}</option>`)
        .join("");
    });
    renderCoverTemplates(cfg.templates || []);
    // 后台设置显示已预填的接入信息
    if ($("apiyiBase")) $("apiyiBase").value = cfg.base_url || "";
    if ($("apiyiKey")) $("apiyiKey").placeholder = cfg.key_ready ? `已预填（${cfg.key_masked}），无需修改` : "未配置";
  } catch (e) {
    log(`AI 封面配置加载失败：${e.message}`);
  }
}

function renderCoverTemplates(templates) {
  const list = $("coverTplList");
  const collapsed = templates.slice(0, 3);
  const render = (arr) => {
    list.innerHTML = arr
      .map((t, i) => `<button class="tpl-chip" data-tpl="${i}" title="${escapeHtml(t)}" type="button">${escapeHtml(t.slice(0, 10))}…</button>`)
      .join("");
    list.querySelectorAll("[data-tpl]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("coverPrompt").value = arr[Number(btn.dataset.tpl)];
      });
    });
  };
  render(collapsed);
  const more = $("coverTplMore");
  more.addEventListener("click", () => {
    const expanded = more.dataset.expanded === "1";
    render(expanded ? collapsed : templates);
    more.dataset.expanded = expanded ? "" : "1";
    more.textContent = expanded ? "•••" : "收起";
  });
}

async function generateCover() {
  const prompt = $("coverPrompt").value.trim();
  if (!prompt) {
    log("请先输入或点选一个封面提示词。");
    return;
  }
  const ref = firstUsableImage();
  if (!ref) {
    log("请先在中间列表选择素材（需要一张参考图）。");
    return;
  }
  const btn = $("coverGenerateBtn");
  btn.disabled = true;
  btn.textContent = "生成中，约 20-60 秒…";
  log("AI 封面生成中（模型 " + $("coverModel").value + " / " + $("coverResolution").value.toUpperCase() + "）…");
  try {
    const data = await api("/api/cover/generate", {
      image_path: ref,
      prompt,
      model: $("coverModel").value,
      resolution: $("coverResolution").value,
      aspect_ratio: "3:4",
    });
    const dataUri = `data:${data.mime};base64,${data.b64}`;
    const img = $("coverPreviewImg");
    img.onload = null;
    img.src = dataUri; // 预览 AI 结果（不覆盖素材）
    img.style.display = "";
    $("previewPlaceholder").style.display = "none";
    img.style.width = "";
    img.style.height = "";
    img.style.maxWidth = "100%";
    img.style.maxHeight = "600px";
    img.style.objectFit = "contain";
    log(`AI 封面已生成（${data.elapsed_sec}s）。${$("coverSaveChk").checked ? "正在保存…" : "勾选「保存到素材同路径」后重新生成即可保存；或点下方保存。"}`);
    if ($("coverSaveChk").checked) await saveCover(data);
  } catch (e) {
    log(`AI 封面生成失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "生成 AI 封面";
  }
}

async function saveCover(data) {
  if (!state.current?.path) return;
  const ts = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}-${pad(ts.getHours())}${pad(ts.getMinutes())}`;
  const folder = `${state.current.path}/AI封面`;
  try {
    const result = await api("/api/save-generated", {
      data_base64: `data:${data.mime};base64,${data.b64}`,
      folder,
      filename: `AI封面_${stamp}.jpg`,
    });
    state.current.selected_images.unshift(result.path); // 插入第一张
    $("imageCountTotal").textContent = `共 ${state.current.selected_images.length} 张`;
    renderThumbStrip(state.current.selected_images);
    updateCoverPreview(result.path);
    log(`已保存并设为封面：${result.path}`);
  } catch (e) {
    log(`保存 AI 封面失败：${e.message}`);
  }
}
