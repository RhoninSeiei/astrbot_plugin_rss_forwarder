const bridge = window.AstrBotPluginPage;
const form = document.getElementById("create-form");
const fields = document.getElementById("create-fields");
const createButton = document.getElementById("create-subscription");
const refreshButton = document.getElementById("refresh");
const urlInput = document.getElementById("rss-url");
const titleInput = document.getElementById("subscription-title");
const intervalInput = document.getElementById("interval-minutes");
const cronInput = document.getElementById("cron-expression");
const targetOptions = document.getElementById("target-options");
const targetsEmpty = document.getElementById("targets-empty");
const statusText = document.getElementById("request-status");
const errorText = document.getElementById("error-text");
const rows = document.getElementById("subscription-rows");
const listEmpty = document.getElementById("list-empty");
const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
const labels = { feeds: "来源", jobs: "轮询任务", daily_digests: "日报" };

let state = null;
let currentKind = "feeds";
let active = true;
let connected = false;

function showError(error) {
  errorText.textContent = error instanceof Error ? error.message : String(error);
  errorText.hidden = false;
}

function clearError() {
  errorText.textContent = "";
  errorText.hidden = true;
}

function applyContext() {
  document.documentElement.dataset.theme = bridge.getContext()?.isDark ? "dark" : "light";
}

function renderControls() {
  const writable = connected && state !== null && !active;
  fields.disabled = !writable;
  createButton.disabled = !writable || !state.targets.some((target) => target.enabled !== false);
  refreshButton.disabled = !connected || active;
  form.setAttribute("aria-busy", String(active));
  document.querySelectorAll(".enabled-toggle").forEach((input) => {
    input.disabled = !writable;
  });
}

function renderScheduleMode() {
  const isCron = document.getElementById("mode-cron").checked;
  document.getElementById("interval-field").hidden = isCron;
  document.getElementById("cron-field").hidden = !isCron;
  intervalInput.disabled = isCron;
  intervalInput.required = !isCron;
  cronInput.disabled = !isCron;
  cronInput.required = isCron;
}

function renderTargets() {
  const selected = new Set(Array.from(
    targetOptions.querySelectorAll("input:checked"), (input) => input.value,
  ));
  targetOptions.replaceChildren();
  for (const target of state.targets) {
    const label = document.createElement("label");
    label.className = "target-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "target_ids";
    input.value = String(target.id);
    input.disabled = target.enabled === false;
    input.checked = !input.disabled && selected.has(input.value);
    const text = document.createElement("span");
    text.textContent = [target.id, target.label || target.name || target.platform, target.enabled === false ? "停用" : ""]
      .filter(Boolean).join(" · ");
    label.append(input, text);
    targetOptions.append(label);
  }
  targetsEmpty.hidden = state.targets.length > 0;
  targetsEmpty.textContent = "暂无推送目标";
}

function scheduleText(item, kind) {
  if (kind === "daily_digests") return item.send_time ? `每日 ${item.send_time}` : "未设置";
  if (item.cron) return `Cron · ${item.cron}`;
  const seconds = Number(item.interval_seconds);
  if (seconds > 0) return seconds % 60 === 0 ? `每 ${seconds / 60} 分钟` : `每 ${seconds} 秒`;
  return "未设置";
}

function sourceText(item) {
  if (currentKind === "feeds") return item.display_url || item.url || item.username || "未设置";
  const feedIds = Array.isArray(item.feed_ids) ? item.feed_ids : [];
  return feedIds.map((id) => {
    const feed = state.feeds.find((entry) => String(entry.id) === String(id));
    return feed?.title ? `${id} · ${feed.title}` : String(id);
  }).join("、") || "未关联来源";
}

function cell(text, label, className = "") {
  const element = document.createElement("td");
  element.textContent = text;
  element.dataset.label = label;
  element.className = className;
  return element;
}

function renderList() {
  const kind = currentKind;
  const sourceLabel = kind === "feeds" ? "地址" : "关联来源";
  document.getElementById("source-column").textContent = sourceLabel;
  document.getElementById("table-caption").textContent = labels[kind];
  rows.replaceChildren();
  const items = state?.[kind] || [];
  for (const item of items) {
    const row = document.createElement("tr");
    const idCell = cell(String(item.id), "ID", "identifier");
    if (item.title) {
      const title = document.createElement("span");
      title.className = "item-title";
      title.textContent = item.title;
      idCell.append(title);
    }
    const schedule = kind === "feeds"
      ? state.jobs.filter((job) => Array.isArray(job.feed_ids) && job.feed_ids.includes(item.id))
        .map((job) => `${job.id} · ${scheduleText(job, "jobs")}`).join("；") || "未关联轮询任务"
      : scheduleText(item, kind);
    const enabledCell = cell("", "启用", "enabled-cell");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "enabled-toggle";
    input.checked = Boolean(item.enabled);
    input.setAttribute("aria-label", `${labels[kind]} ${item.id} 启用`);
    input.addEventListener("change", () => {
      const enabled = input.checked;
      input.checked = Boolean(item.enabled);
      void mutate("subscriptions/toggle", { kind, id: item.id, enabled }, "启用状态已保存。");
    });
    enabledCell.append(input);
    row.append(idCell, cell(sourceText(item), sourceLabel), cell(schedule, "排期"), enabledCell);
    rows.append(row);
  }
  listEmpty.hidden = items.length > 0;
  listEmpty.textContent = state ? `暂无${labels[kind]}` : "订阅状态尚未加载";
  renderControls();
}

async function loadState() {
  // The bridge resolves the API payload directly, as on source-diagnostics.
  const response = await bridge.apiGet("subscriptions/state");
  if (response?.revision == null || !["feeds", "targets", "jobs", "daily_digests"]
    .every((key) => Array.isArray(response[key]))) {
    throw new Error("服务器返回的订阅状态格式无效。");
  }
  state = response;
  document.getElementById("revision").textContent = `修订号 ${state.revision}`;
  renderTargets();
  renderList();
}

function invalidateState() {
  state = null;
  document.getElementById("revision").textContent = "状态未同步";
  targetsEmpty.hidden = false;
  targetsEmpty.textContent = "推送目标尚未同步";
  rows.replaceChildren();
  listEmpty.hidden = false;
  listEmpty.textContent = "订阅状态尚未加载";
}

async function refresh() {
  if (active || !connected) return;
  active = true;
  clearError();
  statusText.textContent = "正在刷新…";
  renderControls();
  try {
    await loadState();
    statusText.textContent = "";
  } catch (error) {
    invalidateState();
    statusText.textContent = "";
    showError(error);
  } finally {
    active = false;
    renderControls();
  }
}

async function mutate(endpoint, body, successMessage) {
  if (active || !state) return;
  active = true;
  clearError();
  statusText.textContent = "正在保存…";
  renderControls();
  let saved = false;
  try {
    await bridge.apiPost(endpoint, { ...body, expected_revision: state.revision });
    saved = true;
    if (endpoint === "subscriptions/create") {
      urlInput.value = "";
      titleInput.value = "";
    }
    statusText.textContent = successMessage;
  } catch (error) {
    statusText.textContent = "";
    showError(error);
  }
  try {
    // Also refresh after rejection: a conflict or transport error may leave a newer revision.
    await loadState();
  } catch (error) {
    const previousError = errorText.hidden ? "" : `${errorText.textContent}\n`;
    invalidateState();
    showError(new Error(`${previousError}${saved ? "保存成功，但状态刷新失败：" : "状态刷新失败："}${error.message || error}`));
  } finally {
    active = false;
    renderControls();
  }
}

function createSubscription(event) {
  event.preventDefault();
  if (active || !state) return;
  clearError();
  statusText.textContent = "";
  if (!form.reportValidity()) return;
  const targetIds = Array.from(targetOptions.querySelectorAll("input:checked"), (input) => input.value);
  if (!targetIds.length) {
    showError(new Error("请至少选择一个推送目标。"));
    return;
  }
  const url = urlInput.value.trim();
  if (!["http:", "https:"].includes(new URL(url).protocol)) {
    showError(new Error("RSS URL 必须使用 HTTP 或 HTTPS。"));
    return;
  }
  const isCron = document.getElementById("mode-cron").checked;
  const seconds = Number(intervalInput.value) * 60;
  if (!isCron && (!Number.isSafeInteger(seconds) || seconds <= 0)) {
    showError(new Error("请输入有效的间隔分钟数。"));
    return;
  }
  if (isCron && !cronInput.value.trim()) {
    showError(new Error("请输入 Cron 表达式。"));
    return;
  }
  void mutate("subscriptions/create", {
    url,
    title: titleInput.value.trim(),
    target_ids: targetIds,
    interval_seconds: isCron ? 0 : seconds,
    cron: isCron ? cronInput.value.trim() : "",
  }, "订阅创建成功。");
}

function selectTab(tab) {
  currentKind = tab.dataset.kind;
  for (const item of tabs) {
    item.setAttribute("aria-selected", String(item === tab));
    item.tabIndex = item === tab ? 0 : -1;
  }
  document.getElementById("subscription-panel").setAttribute("aria-labelledby", tab.id);
  renderList();
}

tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    tabs[next].focus();
    selectTab(tabs[next]);
  });
});
document.querySelectorAll('input[name="schedule-mode"]').forEach((input) => {
  input.addEventListener("change", renderScheduleMode);
});
form.addEventListener("submit", createSubscription);
refreshButton.addEventListener("click", refresh);
renderScheduleMode();

try {
  if (!bridge) throw new Error("AstrBot Plugin Page 桥接不可用。");
  await bridge.ready();
  connected = true;
  applyContext();
  const offContext = bridge.onContext(applyContext);
  window.addEventListener("beforeunload", offContext);
  active = false;
  await refresh();
} catch (error) {
  active = false;
  invalidateState();
  statusText.textContent = "";
  showError(error);
  renderControls();
}
