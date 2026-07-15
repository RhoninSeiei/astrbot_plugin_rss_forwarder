const bridge = window.AstrBotPluginPage;
const I18N_PREFIX = "pages.source-diagnostics";
const DRAFT_SOURCE_ID = "__draft__";
const MODE_NAMES = [
  "direct_strict",
  "proxy_strict",
  "direct_relaxed",
  "proxy_relaxed",
];

const FALLBACKS = {
  title: "来源诊断",
  description: "检查来源在默认网络、来源代理和不同 TLS 校验设置下的可访问性。探测不会保存配置。",
  "headings.source": "来源",
  "headings.draft": "草稿配置",
  "headings.results": "检查结果",
  "headings.recommendation": "配置建议",
  "source.label": "选择来源",
  "source.draft": "草稿来源",
  "source.savedSummary": "保存来源使用后台完整配置；页面仅显示脱敏信息。",
  "source.disabled": "停用",
  "source.empty": "暂无保存来源",
  "sourceTypes.rss": "RSS",
  "sourceTypes.twitter": "Twitter / Nitter",
  "fields.sourceType": "来源类型",
  "fields.sourceAddress": "来源地址",
  "fields.proxyConfigured": "已配置来源代理",
  "fields.currentTls": "当前 TLS 校验",
  "fields.url": "RSS 地址",
  "fields.nitterUrl": "Nitter 基础地址",
  "fields.username": "Twitter 用户名",
  "fields.proxyUrl": "来源代理地址",
  "fields.timeout": "超时秒数",
  "fields.verifySsl": "校验来源 TLS 证书",
  "fields.authMode": "鉴权模式",
  "fields.temporaryKey": "临时 key",
  "fields.fullCheck": "完整检查所有适用模式",
  "auth.none": "无鉴权",
  "auth.query": "查询参数 key",
  "auth.header": "Authorization 请求头",
  "actions.run": "开始探测",
  "actions.running": "正在探测…",
  "results.mode": "模式",
  "results.status": "状态",
  "results.latency": "耗时",
  "results.httpStatus": "HTTP 状态",
  "results.feedRecognition": "内容识别",
  "results.error": "错误",
  "results.none": "—",
  "statuses.success": "成功",
  "statuses.failure": "失败",
  "statuses.skipped": "已跳过",
  "statuses.pending": "待检查",
  "errors.load": "来源列表加载失败",
  "errors.request": "来源探测失败",
  "errors.validation": "请检查草稿字段。",
  "errors.invalidTimeout": "超时必须是 3 至 30 之间的整数。",
  "errors.types.dns": "域名解析",
  "errors.types.connect": "连接",
  "errors.types.timeout": "超时",
  "errors.types.tls_certificate": "TLS 证书",
  "errors.types.proxy": "来源代理",
  "errors.types.http_status": "HTTP 状态",
  "errors.types.invalid_feed": "内容格式",
  "errors.types.unknown": "未知错误",
  "recommendation.none": "完成探测后显示建议。",
  "recommendation.tlsLabel": "TLS 校验",
  "recommendation.proxyLabel": "来源代理",
  "recommendation.keepEnabled": "保持开启",
  "recommendation.disable": "仅该来源可考虑关闭",
  "recommendation.notApplicable": "不适用或保持现状",
  "recommendation.useProxy": "使用当前来源代理",
  "recommendation.noProxy": "无需来源代理",
  "recommendation.proxyUnchanged": "保持现状",
  "recommendation.warningHeading": "证书安全警告",
  "recommendation.securityWarning": "关闭证书校验会失去服务器身份验证，可能遭受中间人攻击。",
  "recommendation.onlyThisSource": "该设置仅应影响当前来源，正文和媒体下载继续使用严格校验。",
  "recommendation.codes.direct_strict": "默认网络与严格证书校验可用。",
  "recommendation.codes.proxy_strict": "来源代理与严格证书校验可用。",
  "recommendation.codes.direct_relaxed": "默认网络仅在关闭证书校验时可用。",
  "recommendation.codes.proxy_relaxed": "来源代理仅在关闭证书校验时可用。",
  "recommendation.codes.invalid_feed": "来源可访问，但响应内容无法识别为受支持的订阅源。",
  "recommendation.codes.unreachable": "所有适用模式均未成功。",
  "values.yes": "是",
  "values.no": "否",
  "values.enabled": "开启",
  "values.disabled": "关闭",
  "values.recognized": "已识别",
  "values.unrecognized": "未识别",
  "values.notApplicable": "不适用",
  "units.seconds": "秒",
  "units.milliseconds": "毫秒",
  "modes.direct_strict": "默认网络 · 严格 TLS",
  "modes.proxy_strict": "来源代理 · 严格 TLS",
  "modes.direct_relaxed": "默认网络 · 放宽 TLS",
  "modes.proxy_relaxed": "来源代理 · 放宽 TLS",
};

const form = document.getElementById("probe-form");
const sourceSelect = document.getElementById("source-select");
const savedSource = document.getElementById("saved-source");
const savedSourceType = document.getElementById("saved-source-type");
const savedSourceUrl = document.getElementById("saved-source-url");
const savedSourceProxy = document.getElementById("saved-source-proxy");
const savedSourceTimeout = document.getElementById("saved-source-timeout");
const savedSourceTls = document.getElementById("saved-source-tls");
const draftFields = document.getElementById("draft-fields");
const sourceTypeInputs = Array.from(
  document.querySelectorAll('input[name="source-type"]'),
);
const rssFields = document.getElementById("rss-fields");
const twitterFields = document.getElementById("twitter-fields");
const sourceUrlInput = document.getElementById("source-url");
const nitterUrlInput = document.getElementById("nitter-url");
const twitterUsernameInput = document.getElementById("twitter-username");
const proxyUrlInput = document.getElementById("proxy-url");
const timeoutInput = document.getElementById("timeout");
const verifySslInput = document.getElementById("verify-ssl");
const authModeInput = document.getElementById("auth-mode");
const temporaryKeyInput = document.getElementById("temporary-key");
const fullCheckInput = document.getElementById("full-check");
const runButton = document.getElementById("run-probe");
const runButtonText = runButton.querySelector("span");
const requestStatus = document.getElementById("request-status");
const errorText = document.getElementById("error-text");
const resultBody = document.getElementById("result-body");
const recommendationMessage = document.getElementById("recommendation-message");
const recommendationFacts = document.getElementById("recommendation-facts");
const recommendationTls = document.getElementById("recommendation-tls");
const recommendationProxy = document.getElementById("recommendation-proxy");
const securityWarning = document.getElementById("security-warning");
const securityWarningText = document.getElementById("security-warning-text");

let feeds = [];
let latestReport = null;
let requestActive = false;

function t(key, fallback = "") {
  return bridge.t(`${I18N_PREFIX}.${key}`, fallback || FALLBACKS[key] || key);
}

function currentSourceType() {
  return sourceTypeInputs.find((input) => input.checked)?.value || "rss";
}

function selectedFeed() {
  return feeds.find((feed) => String(feed.id) === sourceSelect.value) || null;
}

function renderTranslations() {
  const context = bridge.getContext();
  const locale = bridge.getLocale();
  document.documentElement.lang = locale || "zh-CN";
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
  document.title = bridge.t(`${I18N_PREFIX}.title`, FALLBACKS.title);

  document.querySelectorAll("[data-i18n]").forEach((element) => {
    const key = element.dataset.i18n;
    element.textContent = bridge.t(
      `${I18N_PREFIX}.${key}`,
      FALLBACKS[key] || key,
    );
  });

  populateSourceOptions();
  renderSavedSource();
  renderResults(latestReport);
  renderRecommendation(latestReport);
  renderRequestState();
}

function populateSourceOptions() {
  const selected = sourceSelect.value || DRAFT_SOURCE_ID;
  sourceSelect.replaceChildren();

  const draftOption = document.createElement("option");
  draftOption.value = DRAFT_SOURCE_ID;
  draftOption.textContent = t("source.draft");
  sourceSelect.append(draftOption);

  for (const feed of feeds) {
    const option = document.createElement("option");
    option.value = String(feed.id);
    const typeLabel = t(`sourceTypes.${feed.source_type || "rss"}`);
    const disabledLabel = feed.enabled ? "" : ` · ${t("source.disabled")}`;
    option.textContent = `${feed.id} · ${typeLabel}${disabledLabel}`;
    sourceSelect.append(option);
  }

  const hasPreviousSelection = Array.from(sourceSelect.options).some(
    (option) => option.value === selected,
  );
  sourceSelect.value = hasPreviousSelection ? selected : DRAFT_SOURCE_ID;
}

function renderSourceMode() {
  const draftMode = sourceSelect.value === DRAFT_SOURCE_ID;
  draftFields.hidden = !draftMode;
  savedSource.hidden = draftMode;
  if (draftMode) {
    renderSourceFields();
  } else {
    renderSavedSource();
  }
}

function renderSourceFields() {
  const sourceType = currentSourceType();
  rssFields.hidden = sourceType !== "rss";
  twitterFields.hidden = sourceType !== "twitter";
  sourceUrlInput.required = sourceType === "rss";
  twitterUsernameInput.required = sourceType === "twitter";
}

function renderSavedSource() {
  const feed = selectedFeed();
  if (!feed) {
    return;
  }
  savedSourceType.textContent = t(`sourceTypes.${feed.source_type || "rss"}`);
  savedSourceUrl.textContent = feed.display_url || t("results.none");
  savedSourceProxy.textContent = feed.proxy_configured
    ? t("values.yes")
    : t("values.no");
  savedSourceTimeout.textContent = `${Number(feed.timeout)} ${t("units.seconds")}`;
  const isHttps = String(feed.display_url || "").toLowerCase().startsWith("https:");
  savedSourceTls.textContent = isHttps
    ? feed.verify_ssl
      ? t("values.enabled")
      : t("values.disabled")
    : t("values.notApplicable");
}

function buildDraft() {
  const sourceType = currentSourceType();
  const timeout = Number(timeoutInput.value);
  if (!Number.isInteger(timeout) || timeout < 3 || timeout > 30) {
    throw new Error(t("errors.invalidTimeout"));
  }

  const draft = {
    source_type: sourceType,
    proxy_url: proxyUrlInput.value,
    timeout: Number(timeoutInput.value),
    verify_ssl: verifySslInput.checked,
  };

  if (sourceType === "rss") {
    return {
      ...draft,
      url: sourceUrlInput.value,
      auth_mode: authModeInput.value,
      key: temporaryKeyInput.value,
    };
  }

  return {
    ...draft,
    username: twitterUsernameInput.value.trim().replace(/^@+/, ""),
    nitter_url: nitterUrlInput.value,
  };
}

function buildRequestBody() {
  const selectedSourceId = sourceSelect.value;
  const fullCheck = fullCheckInput.checked;
  if (selectedSourceId !== DRAFT_SOURCE_ID) {
    return { feed_id: selectedSourceId, full_check: fullCheck };
  }
  return { draft: buildDraft(), full_check: fullCheckInput.checked };
}

function clearError() {
  errorText.textContent = "";
  errorText.hidden = true;
}

function showError(error) {
  if (error instanceof Error && error.message) {
    errorText.textContent = error.message;
  } else {
    errorText.textContent = t("errors.request");
  }
  errorText.hidden = false;
}

function renderRequestState() {
  runButton.disabled = requestActive;
  runButtonText.textContent = requestActive
    ? t("actions.running")
    : t("actions.run");
  requestStatus.textContent = requestActive ? t("actions.running") : "";
  form.setAttribute("aria-busy", requestActive ? "true" : "false");
}

function resultStatus(attempt, hasReport) {
  if (!attempt) {
    const skippedAttempt = { status: "skipped" };
    return hasReport ? skippedAttempt.status : "pending";
  }
  return attempt.ok ? "success" : "failure";
}

function errorLabel(attempt) {
  if (!attempt?.error_type) {
    return attempt?.error_message || t("results.none");
  }
  const label = t(`errors.types.${attempt.error_type}`);
  return attempt.error_message
    ? `${label}: ${attempt.error_message}`
    : label;
}

function resultCell(value, labelKey, className = "") {
  const cell = document.createElement("td");
  cell.dataset.label = t(labelKey);
  cell.textContent = value;
  if (className) {
    cell.className = className;
  }
  return cell;
}

function renderResults(report) {
  const attempts = Array.isArray(report?.attempts) ? report.attempts : [];
  const attemptsByMode = new Map(
    attempts.map((attempt) => [attempt.mode, attempt]),
  );
  resultBody.replaceChildren();

  for (const mode of MODE_NAMES) {
    const attempt = attemptsByMode.get(mode);
    const status = resultStatus(attempt, Boolean(report));
    const row = document.createElement("tr");
    row.dataset.mode = mode;

    row.append(
      resultCell(t(`modes.${mode}`), "results.mode", "mode-cell"),
      resultCell(t(`statuses.${status}`), "results.status", `status-${status}`),
      resultCell(
        attempt ? `${attempt.latency_ms} ${t("units.milliseconds")}` : t("results.none"),
        "results.latency",
      ),
      resultCell(
        attempt?.http_status == null ? t("results.none") : String(attempt.http_status),
        "results.httpStatus",
      ),
      resultCell(
        attempt
          ? attempt.is_feed
            ? `${t("values.recognized")} · ${attempt.feed_kind || "unknown"}`
            : t("values.unrecognized")
          : t("results.none"),
        "results.feedRecognition",
      ),
      resultCell(
        attempt ? errorLabel(attempt) : t("results.none"),
        "results.error",
        "error-cell breakable",
      ),
    );
    resultBody.append(row);
  }
}

function recommendationText(recommendation) {
  const code = String(recommendation?.code || "unreachable");
  const fallback = recommendation?.message || FALLBACKS[`recommendation.codes.${code}`];
  return bridge.t(
    `${I18N_PREFIX}.recommendation.codes.${code}`,
    fallback || t("recommendation.none"),
  );
}

function renderRecommendation(report) {
  const recommendation = report?.recommendation;
  if (!recommendation) {
    recommendationMessage.textContent = t("recommendation.none");
    recommendationFacts.hidden = true;
    securityWarning.hidden = true;
    return;
  }

  recommendationMessage.textContent = recommendationText(recommendation);
  recommendationFacts.hidden = false;
  if (recommendation.verify_ssl === true) {
    recommendationTls.textContent = t("recommendation.keepEnabled");
  } else if (recommendation.verify_ssl === false) {
    recommendationTls.textContent = t("recommendation.disable");
  } else {
    recommendationTls.textContent = t("recommendation.notApplicable");
  }

  if (recommendation.use_proxy === true) {
    recommendationProxy.textContent = t("recommendation.useProxy");
  } else if (recommendation.use_proxy === false) {
    recommendationProxy.textContent = t("recommendation.noProxy");
  } else {
    recommendationProxy.textContent = t("recommendation.proxyUnchanged");
  }

  const relaxedSucceeded = Array.isArray(report.attempts) && report.attempts.some(
    (attempt) => attempt.ok && String(attempt.mode).endsWith("_relaxed"),
  );
  const showSecurityWarning =
    recommendation.verify_ssl === false && relaxedSucceeded;
  securityWarning.hidden = !showSecurityWarning;
  securityWarningText.textContent = showSecurityWarning
    ? `${t("recommendation.securityWarning")} ${t("recommendation.onlyThisSource")}`
    : "";
}

async function loadFeeds() {
  try {
    const response = await bridge.apiGet("source-probe/feeds");
    feeds = Array.isArray(response) ? response : [];
    populateSourceOptions();
    renderSourceMode();
  } catch (error) {
    showError(error);
    requestStatus.textContent = t("errors.load");
  }
}

async function runProbe(event) {
  event.preventDefault();
  clearError();

  if (sourceSelect.value === DRAFT_SOURCE_ID && !form.reportValidity()) {
    showError(new Error(t("errors.validation")));
    return;
  }

  requestActive = true;
  runButton.disabled = true;
  renderRequestState();
  try {
    const requestBody = buildRequestBody();
    latestReport = await bridge.apiPost("source-probe/run", requestBody);
    renderResults(latestReport);
    renderRecommendation(latestReport);
  } catch (error) {
    showError(error);
  } finally {
    temporaryKeyInput.value = "";
    requestActive = false;
    runButton.disabled = false;
    renderRequestState();
  }
}

sourceSelect.addEventListener("change", renderSourceMode);
sourceTypeInputs.forEach((input) => {
  input.addEventListener("change", renderSourceFields);
});
form.addEventListener("submit", runProbe);

await bridge.ready();
renderTranslations();
renderSourceMode();
const offContext = bridge.onContext(renderTranslations);
window.addEventListener("beforeunload", offContext);
await loadFeeds();
