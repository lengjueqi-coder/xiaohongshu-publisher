const state = {
  config: {},
  candidates: [],
  current: null,
  style: "professional",
  generated: null,
  styleCopies: {}, // 5 种风格预生成结果 { style: {title, content, tags} }
  styleLoading: {},
  copyEpoch: 0,
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
  accounts: [],
  accountBusy: false,
  profileDrafts: new Map(),
  profiles: new Map(),
  profileRequests: new Set(),
  profileLoadId: 0,
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

const UPDATE_POLL_MS = 10 * 60 * 1000;
const UPDATE_FOCUS_MIN_MS = 2 * 60 * 1000;

function renderUpdateBanner(release) {
  const banner = $("updateBanner");
  state.latestUpdate = release;
  $("updateTitle").textContent = `发现新版本 ${release.version}，是否更新？`;
  $("updateNotes").textContent = release.notes || "包含软件功能和问题修复。";
  $("installUpdateBtn").disabled = !release.package_ready;
  $("installUpdateBtn").title = release.package_ready ? "下载更新并重启软件" : "此版本暂缺少完整更新包";
  $("installUpdateBtn").textContent = release.package_ready ? "一键更新并重启" : "更新包准备中";
  $("updateButtonLabel").textContent = "有新版本";
  $("checkUpdatesBtn").classList.toggle("has-update", true);
  $("updateStatus").textContent = `可更新至 ${release.version}`;
  $("updateStatus").classList.toggle("is-error", false);
  banner.hidden = false;
}

async function checkForUpdates(silent = false) {
  if (state.updateCheckPending) return state.updateCheckPending;
  const button = $("checkUpdatesBtn");
  button.disabled = true;
  $("updateButtonLabel").textContent = "正在检查…";
  $("updateStatus").classList.toggle("is-error", false);
  state.lastUpdateCheckAt = Date.now();
  state.updateCheckPending = (async () => {
    try {
      const data = await api("/api/update/check");
      if (data.update_available) {
        renderUpdateBanner(data.release);
        if (!silent) log(`发现新版本 ${data.latest_version}，可在顶部一键更新。`);
      } else {
        state.latestUpdate = null;
        $("updateBanner").hidden = true;
        $("updateButtonLabel").textContent = "检查更新";
        button.classList.toggle("has-update", false);
        $("updateStatus").textContent = `v${data.current_version} · 已是最新`;
        if (!silent) log(`当前已是最新版本 ${data.current_version}。`);
      }
    } catch (error) {
      $("updateButtonLabel").textContent = state.latestUpdate ? "有新版本" : "重试检查更新";
      $("updateStatus").textContent = "检查失败 · 点击重试";
      $("updateStatus").classList.toggle("is-error", true);
      if (!silent) log(error.message);
    } finally {
      button.disabled = false;
    }
  })();
  try { return await state.updateCheckPending; }
  finally { state.updateCheckPending = null; }
}

function maybeCheckForUpdates(minInterval = UPDATE_FOCUS_MIN_MS) {
  if (document.visibilityState === "hidden" || state.updateCheckPending) return;
  if (Date.now() - (state.lastUpdateCheckAt || 0) >= minInterval) checkForUpdates(true);
}

function startUpdateChecks() {
  if (state.updatePollTimer) return;
  checkForUpdates(true);
  state.updatePollTimer = setInterval(() => maybeCheckForUpdates(UPDATE_POLL_MS), UPDATE_POLL_MS);
  document.addEventListener("visibilitychange", () => maybeCheckForUpdates());
  window.addEventListener("focus", () => maybeCheckForUpdates());
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
    button.textContent = "一键更新并重启";
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
    schedule_time: publishMode === "scheduled" ? $("schedulePreview").value.replace(" ", "T") : "",
    profile_dir: state.activeProfile || "Default",
    original: $("originalChk").checked,
    fx_params: fxParamsFromUI(),
    mentions: ($("mentionsInput")?.value || "").split(/\n+/).map((s) => s.trim()).filter(Boolean),
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
  invalidateCopies();
  ["titleInput", "contentInput", "tagsInput"].forEach(id => { $(id).value = ""; });
  $("contentCount").textContent = "0";
  $("copyEngine").textContent = "等待生成";
  $("tagPreview").innerHTML = "";
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
function invalidateCopies() {
  state.copyEpoch += 1;
  state.styleCopies = {};
  state.styleLoading = {};
  const button = $("generateBtn");
  if (button) { button.disabled = false; button.textContent = "重新生成"; }
}

function prefetchAllStyles() {
  // Generate the selected style only; other styles are generated when selected.
  // This avoids five simultaneous paid requests and stale background results.
  generateCopy(state.style, {apply:true, randomize:false}).catch((e) => log(e.message));
}

function renderThumbStrip(selected) {
  $("imageCountTotal").textContent = `共 ${selected.filter(p => !state.excluded.has(p)).length} 张`;
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
  invalidateCopies(); // 换组后文案缓存失效
  renderRecommendation({ ...next, selected_images: next.images.slice(0, 9).sort() });
  state.viewedGroups.add(next.path);
  log(`已换组：${next.name}`);
}

function clearImages() {
  if (!window.confirm("确定清空全部图片吗？\n原图文件保留，仅清空本次编辑区。")) return;
  state.current = null;
  state.excluded = new Set();
  invalidateCopies();
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
      invalidateCopies();
      renderRecommendation({ ...folder, selected_images: folder.images.slice(0, 9).sort() });
      state.viewedGroups.add(folder.path);
    } else {
      // 手动指定的文件夹不在扫描列表里，重新扫描后渲染
      const scanData = await api("/api/scan", { root_path: data.path });
      if (scanData.items[0]) {
        const item = scanData.items[0];
        invalidateCopies();
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
    updateFxPreview();
    return;
  }
  const cover = path || firstUsableImage();
  if (cover) {
    img.onload = () => applyExplicitSize(img, 600); // 显示比例=原图比例
    const draft = state.coverDraft;
    const src = !path && draft?.group === state.current?.path && draft.source === cover ? draft.src : fxQueryParams(cover);
    if (img.getAttribute("src") !== src) img.src = src;
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
  const statusText = (status) => ({ submitted: "平台已接收", submitting: "提交中", reviewed_page: "页面已填入", unknown: "状态不明", failed: "失败", draft: "待审核", paused: "已暂停", cancelled: "已取消" }[status] || "待提交");
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
  showView("publish");
  const images = p.source_images || p.images || [];
  state.current = { path: p.folder_path, name: p.folder_name, selected_images: images };
  state.excluded = new Set();
  renderThumbStrip(images);
  $("titleInput").value = p.title || "";
  $("contentInput").value = p.content || "";
  $("contentCount").textContent = (p.content || "").length;
  $("tagsInput").value = (p.tags || []).map((t) => `#${t}`).join("");
  $("modelName").value = p.model_name || "";
  $("shootingDate").value = p.shooting_date || "";
  $("defaultAlbum").value = p.album || "";
  $("defaultLocation").value = p.location || "";
  $("originalChk").checked = p.original !== false;
  $("visibility").value = p.visibility || "仅自己可见";
  $("publishMode").value = p.publish_mode || (q.schedule_time ? "scheduled" : "immediate");
  $("mentionsInput").value = (p.mentions || []).join("\n");
  $("schedulePreview").value = (q.schedule_time || "").replace("T", " ");
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
    profile_dir: state.activeProfile,
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
    apiyi_base: $("apiyiBase")?.value.trim() || "",
  };
  if ($("deepseekKey").value.trim()) {
    payload.deepseek_api_key = $("deepseekKey").value.trim();
  }
  if ($("apiyiKey")?.value.trim()) payload.apiyi_key = $("apiyiKey").value.trim();
  await api("/api/config", payload);
  if ($("apiyiKey")) {
    if (payload.apiyi_key) $("apiyiKey").placeholder = "已保存，留空保留当前 Key";
    $("apiyiKey").value = "";
  }
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

function copyContext() {
  return `${state.activeProfile}|${state.current?.path || state.current?.name || ""}|${state.copyEpoch}`;
}

function currentText() {
  return JSON.stringify([$("titleInput").value, $("contentInput").value, $("tagsInput").value]);
}

async function generateCopy(style = state.style, opts = { apply: true }) {
  if ($("randomStyleChk").checked && opts.apply && opts.randomize !== false) {
    const styles = ["praise", "professional", "process", "opinion", "story"];
    style = styles[Math.floor(Math.random() * styles.length)];
  }
  if (!state.current) throw new Error("请先推荐一组素材");
  if (opts.apply) state.style = style;
  if (state.styleCopies[style]) {
    if (opts.apply) applyCopy(style);
    return state.styleCopies[style];
  }
  if (state.styleLoading[style]) return state.styleLoading[style];
  const origin = copyContext();
  const before = currentText();
  const button = $("generateBtn");
  if (opts.apply && button) { button.disabled = true; button.textContent = "生成中…"; }
  const pending = (async () => {
    log(`正在调用文案引擎生成「${styleLabel(style)}」…`);
    const data = await api("/api/generate", {
      style, profile_dir: state.activeProfile,
      model_name: $("modelName").value.trim(), notes: $("notes").value.trim(),
      copy_engine: $("copyEngineSelect").value, deepseek_model: $("deepseekModel").value,
      previous_title: $("titleInput").value,
    });
    if (copyContext() !== origin) return null;
    state.generated = data.copy;
    state.styleCopies[style] = {...data.copy, _style:style};
    if (opts.apply && state.style === style && currentText() === before) applyCopy(style);
    log(data.copy.warning || `文案已生成：${data.copy.engine} · ${styleLabel(style)}`);
    return data.copy;
  })();
  state.styleLoading[style] = pending;
  try { return await pending; }
  finally {
    if (state.styleLoading[style] === pending) delete state.styleLoading[style];
    if (opts.apply && button && copyContext() === origin) { button.disabled = false; button.textContent = "重新生成"; }
  }
}

async function polishCurrentCopy() {
  if (!$("titleInput").value.trim() || !$("contentInput").value.trim()) throw new Error("请先填写当前标题和正文");
  const origin = copyContext();
  const before = currentText();
  const style = state.style;
  const button = $("polishBtn");
  button.disabled = true; button.textContent = "润色中…";
  try {
    const data = await api("/api/polish", {
      title: $("titleInput").value, content: $("contentInput").value,
      tags: $("tagsInput").value.split(/[#\s]+/).filter(Boolean),
      profile_dir:state.activeProfile, deepseek_model:$("deepseekModel").value, style:state.style,
    });
    if (copyContext() !== origin || currentText() !== before || state.style !== style) {
      log("当前内容或账号已变化，已保留你的编辑；本次润色未覆盖。"); return;
    }
    state.styleCopies[state.style] = {...data.copy, _style:state.style};
    $("titleInput").value = data.copy.title;
    $("contentInput").value = data.copy.content;
    $("contentCount").textContent = data.copy.content.length;
    $("copyEngine").textContent = data.copy.engine;
    updateChecklist();
    log("已润色当前标题和正文；话题保持原样。");
  } finally { button.disabled = false; button.textContent = "更自然"; }
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
  await generateCopy(style, {apply:true, randomize:false});
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
  await refreshAccountLabel();
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
  const timing = data.result?.elapsed_seconds;
  log(`已填入小红书发布页${timing ? `，耗时 ${timing} 秒` : ""}，历史记录 #${data.history_id}。${warnings}\n${manual}\n请在小红书页面检查后手动点击发布。`);
  await refreshHistory();
}

async function loadNextQueue() {
  await loadQueue();
  if (!state.queue.length) { log("队列中暂无任务。"); return; }
  state.queueIndex = (state.queueIndex + 1) % state.queue.length;
  loadQueueItem(state.queueIndex);
}

async function publishNow() {
  const payload = { ...currentPayload(), publish_mode: "immediate", schedule_time: "" };
  if (!payload.title || !payload.content || !payload.images.length) throw new Error("请先确认图片、标题和正文");
  if (!confirm(`立即发布到当前账号？\n${payload.title}\n${payload.images.length} 张图片 · ${payload.visibility}`)) return;
  await api("/api/xhs/publish-now", payload);
  await loadQueue();
  log("立即发布任务已确认，后台会校验账号与设置后提交；异常时会通知你。");
}

function renderProfile(profile) {
  const box = $("profileBox");
  if (!profile) {
    box.className = "empty profile-box";
    box.textContent = "尚未生成。生成后文案会按账号定位/赛道/受众自动定制（约需 1-2 分钟）。";
    return;
  }
  box.className = "recommend-card profile-box";
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
  const origin = state.activeProfile;
  const requestId = ++state.profileLoadId;
  const data = await api(`/api/profile?profile_dir=${encodeURIComponent(origin)}`);
  if (requestId !== state.profileLoadId || origin !== state.activeProfile || data.profile_dir !== origin) return;
  state.profiles.set(origin, data.profile || null);
  displayProfile();
}

function profileFields() {
  return { manual_keywords: $("profileKeywords").value, manual_topics: $("profileTopics").value, manual_notes: $("profileNotes").value };
}

function displayProfile() {
  const origin = state.activeProfile;
  const profile = state.profiles.get(origin);
  const draft = state.profileDrafts.get(origin) || profile || {};
  $("profileKeywords").value = draft.manual_keywords || "";
  $("profileTopics").value = draft.manual_topics || "";
  $("profileNotes").value = draft.manual_notes || "";
  const pending = state.profileRequests.has(origin);
  $("genProfileBtn").disabled = pending || state.accountBusy;
  $("genProfileBtn").textContent = pending ? "生成中（约1-2分钟）…" : "生成并保存画像";
  $("profileStatus").textContent = pending ? "正在生成，此结果将保存到当前账号" : state.profileDrafts.has(origin) ? "有未保存的修改，生成成功后保存" : profile ? "已保存，后续 AI 文案会自动使用" : "尚未生成画像";
  renderProfile(profile);
  if (pending) $("profileBox").textContent = "正在读取创作平台数据并生成画像…";
}

async function generateProfile() {
  const origin = state.activeProfile;
  if (state.profileRequests.has(origin) || state.accountBusy) return;
  const fields = profileFields();
  state.profileDrafts.set(origin, fields);
  state.profileRequests.add(origin);
  displayProfile();
  log("正在生成账号画像，需要 1-2 分钟...");
  try {
    const data = await api("/api/profile", {
      max_count: 20,
      profile_dir: origin,
      ...fields,
    });
    if (data.profile_dir !== origin) throw new Error("账号画像归属不一致，请重新加载");
    state.profiles.set(origin, data.profile);
    if (JSON.stringify(state.profileDrafts.get(origin)) === JSON.stringify(fields)) state.profileDrafts.delete(origin);
    log("账号画像已生成，后续文案将按画像定制。");
  } finally {
    state.profileRequests.delete(origin);
    if (state.activeProfile === origin) displayProfile();
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
    logo_opacity: state.logo.opacity,
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
  q.set("profile_dir", state.activeProfile);
  Object.entries(p).forEach(([k, v]) => q.set(k, v));
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
    await api("/api/logo/settings", {...fxParamsFromUI(),profile_dir:state.activeProfile});
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
  clearTimeout(state.previewDebounce);
  state.previewDebounce = setTimeout(async () => {
    if (activeRightTab === "cover") updateCoverPreview();
    else updateFxPreview();
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
      const data = await api("/api/logo/upload", { kind, data_base64: reader.result, profile_dir:state.activeProfile });
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
    const data = await api(`/api/logo/settings?profile_dir=${encodeURIComponent(state.activeProfile)}`);
    const s = data.settings || {};
    state.logo.enabled = !!s.logo_enabled;
    state.logo.path = s.logo_path || "";
    state.watermark.text = s.watermark_text || "";
    state.watermark.path = s.watermark_path || "";
    state.watermark.enabled = !!s.watermark_enabled;
    const setVal = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
    setVal("watermarkTextInput", state.watermark.text);
    if (s.watermark_text) {
      state.watermark.text = s.watermark_text;
      state.watermark.enabled = true;
      setVal("watermarkTextInput", s.watermark_text);
    } else if (s.watermark_path) {
      state.watermark.path = s.watermark_path; // 旧 PNG 水印仅保留路径，不再启用
    }
    state.logo.scale = s.logo_scale ?? 18;
    state.logo.opacity = s.logo_opacity ?? 100;
    const pos = String(s.logo_position || "").match(/^([\d.]+)%,([\d.]+)%$/);
    const anchors = {tl:[8,8],tc:[8,50],tr:[8,92],ml:[50,8],center:[50,50],mr:[50,92],bl:[92,8],bc:[92,50],br:[92,92]};
    [state.logo.vpos, state.logo.hpos] = pos ? [Number(pos[1]),Number(pos[2])] : (anchors[s.logo_position] || [92,92]);
    state.watermark.scale = s.watermark_scale ?? 120;
    state.watermark.opacity = s.watermark_opacity ?? 40;
    setVal("logoScale", state.logo.scale);
    setVal("logoOpacityNew", state.logo.opacity);
    setVal("logoVPos", state.logo.vpos);
    setVal("logoHPos", state.logo.hpos);
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
  $("collageAutoFillBtn").textContent = `一键填入前 ${n*n} 张`;
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
  $("appShell").classList.toggle("automation-view", name === "automation");
  ["publish", "records", "settings", "profile", "automation"].forEach((v) => {
    const el = $(`page${v[0].toUpperCase()}${v.slice(1)}`);
    if (el) el.style.display = v === name ? "" : "none";
  });
  $("bottomBar").hidden = name !== "publish";
  document.querySelectorAll("[data-nav]").forEach((el) => {
    el.classList.toggle("active", el.dataset.nav === name);
    if (el.dataset.nav === name) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  const main = document.querySelector(".main-area");
  if (main) main.scrollTop = 0;
  if (name === "automation") window.loadAutomation?.();
}

function handleNav(target) {
  if (["publish", "settings", "records", "profile", "automation"].includes(target)) {
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
  shell.style.setProperty("--auto-sidebar-width", `${lw}px`);
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
  $("planProgress").textContent = Math.min(state.queue.filter(q => q.status !== "cancelled").length, total);
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
  return (state.current.selected_images || []).find((p) => !state.excluded.has(p)) || null;
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
    const maxW = Math.min(Math.max(1, ((parent && parent.clientWidth) || 420) - 14), maxWCAP);
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
  if (!["watermark", "logo"].includes(activeRightTab)) return;
  const first = firstUsableImage();
  const box = $("fxPreviewBox");
  const base = $("fxBaseImg");
  $("fxWatermarkLayer").style.display = "none";
  $("fxLogoLayer").style.display = "none";
  if (!first) { box.style.display = "none"; return; }
  box.style.display = "";
  box.style.aspectRatio = "";
  const url = fxQueryParams(first);
  if (base.getAttribute("src") !== url) {
    $("fxHint").textContent = "正在合成实际发布效果…";
    base.onload = () => { applyExplicitSize(base); $("fxHint").textContent = "与发布图片使用相同合成规则"; };
    base.onerror = () => { $("fxHint").textContent = "预览加载失败，请重试"; };
    base.src = url;
  } else applyExplicitSize(base);
}

function switchRightTab(tab) {
  activeRightTab = tab;
  $("rightPreview")?.classList.toggle("collage-mode", tab === "collage");
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
    updateCoverPreview();
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
  $("generateBtn")?.addEventListener("click", () => { invalidateCopies(); generateCopy().catch((e) => log(e.message)); });
  $("polishBtn")?.addEventListener("click", () => polishCurrentCopy().catch((e) => log(e.message)));
  ["notes", "modelName", "copyEngineSelect", "deepseekModel"].forEach((id) => {
    $(id)?.addEventListener("change", invalidateCopies);
  });
  document.querySelectorAll("[data-style]").forEach((btn) => {
    btn.addEventListener("click", () => selectStyle(btn.dataset.style).catch((e) => log(e.message)));
  });

  // 正文计数
  $("contentInput").addEventListener("input", () => {
    $("contentCount").textContent = $("contentInput").value.length;
  });

  // 底栏：立即发布 / 定时入队
  $("immediatePublishBtn").addEventListener("click", () => publishNow().catch((e) => log(e.message)));
  $("prefillPublishBtn").addEventListener("click", () => fillPublish().catch((e) => log(e.message)));
  $("mentionFocusBtn").addEventListener("click", () => $("mentionsInput").focus());
  $("schedulePublishBtn").addEventListener("click", () => enqueueCurrent().catch((e) => log(e.message)));
  $("nextQueueBtn").addEventListener("click", () => loadNextQueue().catch((e) => log(e.message)));
  $("startQueueBtn").addEventListener("click", () => runQueue().catch((e) => log(e.message)));
  $("pauseQueueBtn").addEventListener("click", () => pauseQueue().catch((e) => log(e.message)));

  // 定时弹窗
  $("dialogCancelBtn").addEventListener("click", () => { $("scheduleDialog").style.display = "none"; });
  $("dialogConfirmBtn").addEventListener("click", () => confirmEnqueue().catch((e) => log(e.message)));

  // 历史 / 画像
  $("refreshHistoryBtn").addEventListener("click", () => refreshHistory().catch((e) => log(e.message)));
  $("genProfileBtn").addEventListener("click", () => generateProfile().catch((e) => log(e.message)));
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
  startUpdateChecks();
  ["profileKeywords", "profileTopics", "profileNotes"].forEach((id) => $(id).addEventListener("input", () => {
    state.profileDrafts.set(state.activeProfile, profileFields());
    $("profileStatus").textContent = "有未保存的修改，生成成功后保存";
  }));
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
  await initAccountBar();
  initCoverPanel();
  log(`工作台已就绪，当前版本 ${state.config.app_version || "未知"}。`);
}

init().catch((err) => log(err.message));

// ---------- 右上角多账号（Chrome Profile 隔离，每个账号独立记住登录态） ----------

function closeAccountMenu() {
  $("accountMenu").style.display = "none";
  $("accountBtn").setAttribute("aria-expanded", "false");
}

function identityLabel(identity = {}) {
  if (identity.status === "logged_out") return "小红书未登录";
  return identity.nickname || "小红书账号待识别";
}

function identityStatus(identity = {}) {
  if (identity.status === "logged_out") return "请在此 Chrome 用户资料中登录小红书";
  if (identity.status === "unavailable") return identity.nickname ? "昵称为上次识别的缓存，请连接后刷新" : "请打开对应 Chrome 用户资料并连接 XHS Bridge 后刷新";
  return identity.checked_at ? `上次识别：${identity.checked_at}` : "连接后点「刷新当前账号资料」读取昵称和头像";
}

function avatarHtml(identity = {}, label = "") {
  const fallback = escapeHtml((identity.nickname || label || "账").slice(0, 1));
  return identity.avatar ? `<img src="${escapeHtml(identity.avatar)}" alt="" referrerpolicy="no-referrer" loading="lazy" />` : fallback;
}

function bindAvatarFallback(root) {
  root.querySelectorAll(".account-avatar img").forEach((img) => {
    img.addEventListener("error", () => { img.parentElement.textContent = "账"; }, { once: true });
  });
}

function renderCurrentAccount() {
  const p = state.accounts.find((x) => x.dir === state.activeProfile);
  const identity = p?.identity || {};
  const label = p?.label || state.activeProfile;
  const nickname = identityLabel(identity);
  $("accountName").textContent = `Chrome · ${label}`;
  $("accountNickname").textContent = nickname;
  $("accountAvatar").innerHTML = avatarHtml(identity, label);
  $("accountDot").classList.toggle("ok", identity.status === "verified");
  $("accountDot").title = identityStatus(identity);
  $("accountBtn").title = `${label} → ${nickname}\n${identityStatus(identity)}`;
  $("profileAvatar").innerHTML = avatarHtml(identity, label);
  $("profileAccountName").textContent = `${label} → ${nickname}`;
  $("profileAccountStatus").textContent = identityStatus(identity);
  bindAvatarFallback(document.querySelector(".workspace-header"));
  bindAvatarFallback($("pageProfile"));
}

async function initAccountBar() {
  $("accountBtn").addEventListener("click", async () => {
    const menu = $("accountMenu");
    if (menu.style.display !== "none") {
      closeAccountMenu();
      return;
    }
    menu.style.display = "";
    $("accountBtn").setAttribute("aria-expanded", "true");
    menu.innerHTML = '<div class="account-item muted">读取中…</div>';
    try {
      const data = await api("/api/accounts/list", {});
      await acceptAccountList(data);
      renderAccountMenu(data);
    } catch (e) {
      menu.innerHTML = `<div class="account-item muted">${escapeHtml(e.message)}</div>`;
    }
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".account-bar")) closeAccountMenu();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAccountMenu();
  });
  await refreshAccountLabel();
  // Read only the connected account; never switch through profiles to populate labels.
  refreshAccountIdentity(true);
}

function renderAccountMenu(data) {
  const menu = $("accountMenu");
  const rows = (data.profiles || [])
    .map((p) => {
      const active = p.dir === data.active;
      const identity = p.identity || {};
      return `
        <button class="account-item${active ? " is-active" : ""}" data-profile="${escapeHtml(p.dir)}" type="button" ${state.accountBusy ? "disabled" : ""} title="${escapeHtml(identityStatus(identity))}" ${active ? 'aria-current="true"' : ""}>
          <span class="account-check">${active ? "✓" : ""}</span>
          <span class="account-avatar" aria-hidden="true">${avatarHtml(identity, p.label)}</span>
          <span class="account-names"><span class="account-chrome">Chrome · ${escapeHtml(p.label)}</span><span class="account-nickname">${escapeHtml(identityLabel(identity))}</span></span>
          <span class="account-dir">${identity.status === "verified" ? "已识别" : identity.nickname ? "已缓存" : ""}</span>
        </button>`;
    })
    .join("");
  menu.innerHTML = `
    <div class="account-head">Chrome 用户资料 → 小红书运营账号</div>
    ${rows || '<div class="account-item muted">未检测到 Chrome 账号</div>'}
    <div class="account-sep"></div>
    <button class="account-item" id="accountRefreshBtn" type="button" ${state.accountBusy ? "disabled" : ""}>刷新当前账号资料</button>
    <button class="account-item" id="accountProfileBtn" type="button">管理当前账号画像</button>
    <button class="account-item" id="accountLogoutBtn" type="button" ${state.accountBusy ? "disabled" : ""}>退出当前小红书账号（清登录态）</button>
    <div class="account-tip">每个 Chrome 用户资料独立保存登录态。未连接的账号显示已缓存资料；在 Chrome 手动更换小红书账号后，请刷新资料。添加账号请先在 Chrome 新建用户资料并登录小红书。</div>
  `;
  menu.querySelectorAll("[data-profile]").forEach((btn) => {
    btn.addEventListener("click", () => switchAccount(btn.dataset.profile, state.accounts.find((p) => p.dir === btn.dataset.profile)?.label || btn.dataset.profile));
  });
  $("accountRefreshBtn").addEventListener("click", () => refreshAccountIdentity());
  $("accountProfileBtn").addEventListener("click", () => { closeAccountMenu(); showView("profile"); });
  const logout = $("accountLogoutBtn");
  if (logout) logout.addEventListener("click", logoutAccount);
  bindAvatarFallback(menu);
}

async function acceptAccountList(data) {
  const changed = data.active && data.active !== state.activeProfile;
  state.accounts = data.profiles || [];
  state.activeProfile = data.active || state.activeProfile;
  renderCurrentAccount();
  if (changed) {
    invalidateCopies();
    displayProfile();
    await loadConfig();
    await loadFxSettings();
    await loadProfile();
    window.loadAutomation?.();
  }
}

async function refreshAccountLabel() {
  const data = await api("/api/accounts/list", {});
  await acceptAccountList(data);
  return data;
}

async function refreshAccountIdentity(silent = false) {
  if (state.accountBusy) return;
  state.accountBusy = true;
  displayProfile();
  if (!silent) log("正在读取当前连接的小红书昵称和头像…");
  renderAccountMenu({ profiles: state.accounts, active: state.activeProfile });
  try {
    const data = await api("/api/accounts/refresh", { profile: state.activeProfile });
    await refreshAccountLabel();
    if (!silent) log(data.identity?.status === "verified" ? `已识别小红书账号：${data.identity.nickname}` : data.identity?.error || "当前小红书账号未登录");
  } catch (e) {
    if (!silent) log(`账号资料刷新失败：${e.message}`);
  } finally {
    state.accountBusy = false;
    displayProfile();
    renderAccountMenu({ profiles: state.accounts, active: state.activeProfile });
  }
}

async function switchAccount(profileDir, label) {
  if (state.accountBusy) return;
  closeAccountMenu();
  if (profileDir === state.activeProfile) return;
  state.accountBusy = true;
  displayProfile();
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
    await refreshAccountLabel();
  } catch (e) {
    setBridge("切换失败", false);
    log(`切换账号失败：${e.message}`);
    await refreshAccountLabel().catch(() => {});
  } finally {
    state.accountBusy = false;
    displayProfile();
  }
}

async function logoutAccount() {
  if (state.accountBusy) return;
  if (!confirm("确定退出当前账号？将清除这个浏览器账号的小红书登录状态（其他账号不受影响）。")) return;
  closeAccountMenu();
  state.accountBusy = true;
  try {
    await api("/api/accounts/logout", {});
    await refreshAccountLabel();
    log("已退出当前账号并清除其登录状态。");
  } catch (e) {
    log(`退出失败：${e.message}`);
  } finally {
    state.accountBusy = false;
  }
}

// ---------- AI 封面（API易原生图生图；接入信息保存于本机） ----------

function initCoverPanel() {
  loadCoverConfig();
  $("coverGenerateBtn").addEventListener("click", generateCover);
  $("coverSaveBtn").addEventListener("click", () => {
    if (state.coverResult) saveCover(state.coverResult.data, state.coverResult.target);
  });
}

async function loadCoverConfig() {
  try {
    const cfg = await api("/api/cover/config", {});
    $("coverPlatform").innerHTML = cfg.platforms
      .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`).join("");
    const modelSel = $("coverModel");
    const previousModel = modelSel.value;
    const previousResolution = $("coverResolution").value;
    modelSel.innerHTML = cfg.models
      .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("");
    modelSel.value = cfg.models.some((m) => m.id === previousModel)
      ? previousModel : cfg.default_model || cfg.models[0]?.id || "";
    const updateResolutions = (preferred) => {
      const model = cfg.models.find((m) => m.id === modelSel.value);
      if (!model) return;
      $("coverResolution").innerHTML = model.resolutions
        .map((r) => `<option value="${r}">${r.toUpperCase()}</option>`).join("");
      $("coverResolution").value = model.resolutions.includes(preferred) ? preferred : model.default_resolution;
    };
    updateResolutions(previousResolution);
    modelSel.onchange = () => updateResolutions("");
    renderCoverTemplates(cfg.templates || []);
    if ($("apiyiBase")) $("apiyiBase").value = cfg.base_url || "";
    if ($("apiyiKey")) $("apiyiKey").placeholder = cfg.key_ready ? "已保存，留空保留当前 Key" : "请填入 API易 Key 并保存";
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

function coverTargetIsCurrent(target) {
  return state.current?.path === target.group && state.activeProfile === target.profile;
}

async function generateCover() {
  const prompt = $("coverPrompt").value.trim();
  if (!prompt) { log("请先输入或点选一个封面提示词。"); return; }
  const ref = firstUsableImage();
  if (!ref || !state.current?.path) { log("请先选择一组素材和参考图。"); return; }
  const target = {group: state.current.path, profile: state.activeProfile, source: ref};
  const autoSave = $("coverSaveChk").checked;
  const btn = $("coverGenerateBtn");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "正在生成，4K 可能需要数分钟…";
  log("AI 封面生成中（" + $("coverModel").value + " / " + $("coverResolution").value.toUpperCase() + "），请勿重复点击。");
  try {
    const data = await api("/api/cover/generate", {
      platform: $("coverPlatform").value,
      image_path: ref, prompt, model: $("coverModel").value,
      resolution: $("coverResolution").value, aspect_ratio: "3:4",
    });
    state.coverResult = {data, target};
    $("coverSaveBtn").hidden = false;
    $("coverSaveBtn").disabled = false;
    $("coverSaveBtn").textContent = "保存本次 AI 封面";
    if (coverTargetIsCurrent(target) && firstUsableImage() === ref) {
      const dataUri = `data:${data.mime};base64,${data.b64}`;
      const img = $("coverPreviewImg");
      state.coverDraft = {group: target.group, source: ref, src: dataUri};
      img.onload = () => applyExplicitSize(img, 600);
      img.src = dataUri;
      img.style.display = "";
      $("previewPlaceholder").style.display = "none";
      log(`AI 封面已生成（${data.elapsed_sec}s，${data.width}×${data.height}）。可直接保存本次结果。`);
    } else {
      log("AI 封面已生成；你已切换素材或账号，可点击保存本次结果到原素材文件夹。");
    }
    if (autoSave) await saveCover(data, target);
  } catch (e) {
    log(`AI 封面生成失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "生成 AI 封面";
  }
}

async function saveCover(data, target) {
  if (!target?.group) return;
  const btn = $("coverSaveBtn");
  if (btn.disabled) return;
  btn.disabled = true;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  try {
    const result = await api("/api/save-generated", {
      data_base64: `data:${data.mime};base64,${data.b64}`,
      folder: `${target.group}/AI封面`, filename: `AI封面_${stamp}.${data.mime === "image/png" ? "png" : data.mime === "image/webp" ? "webp" : "jpg"}`,
    });
    if (coverTargetIsCurrent(target)) {
      state.current.selected_images.unshift(result.path);
      $("imageCountTotal").textContent = `共 ${state.current.selected_images.length} 张`;
      renderThumbStrip(state.current.selected_images);
      updateCoverPreview(result.path);
    }
    btn.textContent = "本次 AI 封面已保存";
    log(`AI 封面已保存：${result.path}`);
  } catch (e) {
    btn.disabled = false;
    log(`保存 AI 封面失败：${e.message}。本次生成结果仍保留，可再次点击保存。`);
  }
}
