const form = document.querySelector("#settings-form");
const settingsStatus = document.querySelector("#settings-status");
const manualForm = document.querySelector("#manual-form");
const manualStatus = document.querySelector("#manual-status");
const testResult = document.querySelector("#test-result");
const manualTranscriptsRoot = document.querySelector("#manual-transcripts");
const questionsRoot = document.querySelector("#questions");
const filteredRoot = document.querySelector("#filtered-messages");
const filteredStatus = document.querySelector("#filtered-status");
const memoryForm = document.querySelector("#memory-form");
const memoriesRoot = document.querySelector("#memories");
const memoryStatus = document.querySelector("#memory-status");
const shutdownButton = document.querySelector("#shutdown-agent");
const agentStatus = document.querySelector("#agent-status");
let currentWechatInputMode;
let lastActivitySnapshot;
let activityRefreshInProgress = false;

shutdownButton.addEventListener("click", async () => {
  if (!window.confirm("关闭 Agent 会同时停止本地界面和后台收件任务。确定继续吗？")) return;
  shutdownButton.disabled = true;
  shutdownButton.textContent = "正在关闭...";
  agentStatus.textContent = "正在关闭 Agent...";
  try {
    await api("/api/shutdown", { method: "POST", body: JSON.stringify({}) });
    window.close();
    if (!window.closed) window.location.replace("about:blank");
  } catch (error) {
    shutdownButton.disabled = false;
    shutdownButton.textContent = "关闭 Agent";
    agentStatus.textContent = "仅本机运行";
    window.alert(`关闭 Agent 失败：${error.message}`);
  }
});

function updateSIWXSettingsVisibility() {
  const isSIWX = form.elements.namedItem("wechat_input_mode").value === "siwx";
  document.querySelectorAll("[data-siwx-only]").forEach((field) => {
    field.hidden = !isSIWX;
  });
}

form.elements.namedItem("wechat_input_mode").addEventListener(
  "change",
  updateSIWXSettingsVisibility,
);

// Centralize JSON error handling so every form reports server-side validation consistently.
// 集中处理 JSON 响应和错误，让各表单都能显示服务端校验信息。
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

// Fill only public preferences; secret inputs intentionally remain blank after reload.
// 只回填公开设置；密钥输入框在刷新后保持为空，避免回显凭据。
async function loadSettings() {
  const values = await api("/api/settings");
  currentWechatInputMode = values.wechat_input_mode;
  document.querySelector("#config-file").textContent = values.config_name || "settings.json";
  form.elements.namedItem("profile_id").readOnly = Boolean(values.profile_name_bound);
  for (const [name, value] of Object.entries(values)) {
    // Checkbox state is not represented by the value attribute used by normal inputs.
    // 复选框需要更新 checked 状态，不能像普通文本框那样赋给 value。
    const input = form.elements.namedItem(name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
  updateSIWXSettingsVisibility();
}

// Rebuild the editor from the active profile's local memory store.
// 从当前 profile 的本地记忆存储重建编辑列表。
async function loadMemories() {
  const { memories } = await api("/api/memories");
  memoriesRoot.replaceChildren();
  if (!memories.length) {
    memoriesRoot.append(element("div", "empty", "当前配置还没有记忆。"));
    return;
  }
  for (const memory of memories) {
    // Each editor keeps the memory ID used by its profile-scoped update/delete routes.
    // 每个编辑项保留对应 ID，写入时由后端限定在当前 profile 内。
    const item = element("article", "memory-entry");
    const heading = element("div", "memory-meta", `${memory.decision} · ${memory.source} · ${new Date(memory.updated_at).toLocaleString()}`);
    const content = element("textarea");
    content.rows = 3;
    content.maxLength = 4000;
    content.value = memory.content;
    const controls = element("div", "memory-controls");
    const decision = document.createElement("select");
    for (const value of ["补充", "满足", "不满足"]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      decision.append(option);
    }
    decision.value = memory.decision;
    const tags = document.createElement("input");
    tags.value = memory.tags.join(", ");
    tags.setAttribute("aria-label", "记忆标签");
    const enabledLabel = element("label", "memory-toggle", "启用");
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = memory.enabled;
    enabledLabel.prepend(enabled);
    const save = element("button", "button quiet", "保存修改");
    save.type = "button";
    save.addEventListener("click", async () => {
      try {
        await api(`/api/memories/${memory.memory_id}`, {
          method: "POST",
          body: JSON.stringify({
            content: content.value,
            decision: decision.value,
            tags: tags.value.split(",").map((tag) => tag.trim()).filter(Boolean),
            enabled: enabled.checked,
          }),
        });
        memoryStatus.textContent = "记忆已更新。";
        await loadMemories();
      } catch (error) {
        memoryStatus.textContent = error.message;
      }
    });
    const remove = element("button", "button danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/memories/${memory.memory_id}`, { method: "DELETE" });
        memoryStatus.textContent = "记忆已删除。";
        await loadMemories();
      } catch (error) {
        memoryStatus.textContent = error.message;
      }
    });
    controls.append(decision, tags, enabledLabel, save, remove);
    item.append(heading, content, controls);
    memoriesRoot.append(item);
  }
}

memoryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(memoryForm);
  memoryStatus.textContent = "正在保存...";
  try {
    await api("/api/memories", {
      method: "POST",
      body: JSON.stringify({
        content: values.get("content"),
        decision: values.get("decision"),
        tags: String(values.get("tags") || "").split(",").map((tag) => tag.trim()).filter(Boolean),
      }),
    });
    memoryForm.reset();
    memoryStatus.textContent = "记忆已保存。";
    await loadMemories();
  } catch (error) {
    memoryStatus.textContent = error.message;
  }
});

// Send secrets separately so they never enter settings JSON or form persistence.
// 单独提交密钥，避免其进入普通设置 JSON 或持久化配置。
async function saveSecret(name, input) {
  if (!input.value) return;
  const values = Object.fromEntries(new FormData(form).entries());
  const credential = { name, secret: input.value };
  if (name === "llm_api_key") credential.model_url = values.llm_base_url || "";
  if (name === "qq_mail_app_password") credential.mail_username = values.mail_username || "";
  await api("/api/secrets", {
    method: "POST",
    body: JSON.stringify(credential),
  });
  input.value = "";
}

form.addEventListener("submit", async (event) => {
  // FormData omits unchecked checkboxes, so serialize that boolean explicitly.
  // FormData 会省略未勾选复选框，因此显式写出布尔值。
  event.preventDefault();
  settingsStatus.textContent = "正在保存...";
  try {
    const values = Object.fromEntries(new FormData(form).entries());
    values.dedup_retention_days = Number(values.dedup_retention_days);
    values.filtered_retention_days = Number(values.filtered_retention_days);
    values.mail_ingestion_enabled = form.elements.namedItem("mail_ingestion_enabled").checked;
    values.siwx_auto_export_enabled = values.wechat_input_mode === "siwx"
      && form.elements.namedItem("siwx_auto_export_enabled").checked;
    values.siwx_auto_cleanup_enabled = form.elements.namedItem("siwx_auto_cleanup_enabled").checked;
    values.mail_imap_port = Number(values.mail_imap_port);
    values.mail_smtp_port = Number(values.mail_smtp_port);
    values.poll_interval_seconds = Number(values.poll_interval_seconds);
    values.siwx_auto_export_interval_days = Number(values.siwx_auto_export_interval_days);
    values.siwx_auto_cleanup_retention_days = Number(values.siwx_auto_cleanup_retention_days);
    const modeChanged = values.wechat_input_mode !== currentWechatInputMode;
    const profileNameUnbound = !form.elements.namedItem("profile_id").readOnly;
    if (modeChanged || profileNameUnbound) {
      // Save secrets before a mode/profile change can trigger Agent shutdown.
      // 输入方式或 profile 变更可能关闭 Agent，因此先保存本次输入的凭据。
      await saveSecret("llm_api_key", document.querySelector("#llm-secret"));
      await saveSecret("qq_mail_app_password", document.querySelector("#mail-secret"));
    }
    const savedSettings = await api("/api/settings", { method: "POST", body: JSON.stringify(values) });
    document.querySelector("#config-file").textContent = savedSettings.config_name || "settings.json";
    form.elements.namedItem("profile_id").readOnly = Boolean(savedSettings.profile_name_bound);
    if (!modeChanged && !profileNameUnbound) {
      await saveSecret("llm_api_key", document.querySelector("#llm-secret"));
      await saveSecret("qq_mail_app_password", document.querySelector("#mail-secret"));
    }
    settingsStatus.textContent = savedSettings.restart_required
      ? `配置已保存为 ${savedSettings.config_name}，Agent 即将关闭。请使用该配置重新启动。`
      : "设置已保存到本机。";
    currentWechatInputMode = values.wechat_input_mode;
  } catch (error) {
    settingsStatus.textContent = error.message;
  }
});

function setManualFormBusy(busy) {
  manualForm.querySelector('[type="submit"]').disabled = busy;
  manualTranscriptsRoot.querySelectorAll("textarea").forEach((textarea) => {
    textarea.disabled = busy;
  });
  updateManualTranscriptRows(busy);
}

function updateManualTranscriptRows(busy = false) {
  const rows = Array.from(manualTranscriptsRoot.children);
  rows.forEach((row, index) => {
    row.querySelector("[data-transcript-label]").textContent = `聊天记录 ${index + 1}`;
    row.querySelector("[data-remove-transcript]").disabled = busy || rows.length === 1;
  });
  document.querySelector("#add-manual-transcript").disabled = busy || rows.length >= 20;
}

function addManualTranscriptRow() {
  if (manualTranscriptsRoot.children.length >= 20) {
    manualStatus.textContent = "每次最多添加 20 段聊天记录。";
    return;
  }
  const row = document.createElement("div");
  row.className = "manual-transcript-row";
  const label = document.createElement("label");
  label.className = "field";
  const labelText = element("span", "", "聊天记录");
  labelText.dataset.transcriptLabel = "true";
  const textarea = document.createElement("textarea");
  textarea.dataset.manualTranscript = "true";
  textarea.rows = 10;
  textarea.maxLength = 200_000;
  textarea.placeholder = "粘贴一段完整聊天记录；一段记录中可以包含多条目标信息和杂讯。";
  textarea.required = true;
  label.append(labelText, textarea);
  const remove = element("button", "button quiet", "移除");
  remove.type = "button";
  remove.dataset.removeTranscript = "true";
  row.append(label, remove);
  manualTranscriptsRoot.append(row);
  updateManualTranscriptRows();
  textarea.focus();
}

document.querySelector("#add-manual-transcript").addEventListener(
  "click",
  addManualTranscriptRow,
);
updateManualTranscriptRows();
manualTranscriptsRoot.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove-transcript]");
  if (!remove || manualTranscriptsRoot.children.length === 1) return;
  remove.closest(".manual-transcript-row").remove();
  updateManualTranscriptRows();
});

function renderManualResults(groups, transcripts) {
  const statusLabels = {
    delivered: "邮件已发送",
    filtered: "已归入过滤信息",
    needs_input: "已加入待确认",
    duplicate: "重复消息，已跳过",
    error: "处理失败",
  };
  testResult.replaceChildren();
  for (const group of groups) {
    const transcriptIndex = group.transcript_index - 1;
    if (!group.segments.length) {
      const failure = group.results[0];
      const card = element("article", "manual-result");
      card.append(element("h3", "manual-result-heading", `聊天记录 ${group.transcript_index} · 拆分失败`));
      card.append(element("pre", "manual-result-content", transcripts[transcriptIndex]));
      if (failure?.error) card.append(element("p", "manual-result-error", failure.error));
      testResult.append(card);
      continue;
    }
    for (const [index, result] of group.results.entries()) {
      const card = element("article", "manual-result");
      const outcome = statusLabels[result.status] || "已处理";
      card.append(element(
        "h3",
        "manual-result-heading",
        `聊天记录 ${group.transcript_index} · 片段 ${index + 1} · ${result.decision || "无分类"} · ${outcome}`,
      ));
      card.append(element("pre", "manual-result-content", group.segments[index]));
      if (result.explanation) card.append(element("p", "manual-result-detail", result.explanation));
      if (result.error) card.append(element("p", "manual-result-error", result.error));
      if (result.evidence?.length) {
        card.append(element("p", "manual-result-detail", `依据：${result.evidence.join("；")}`));
      }
      if (result.missing_fields?.length) {
        card.append(element("p", "manual-result-detail", `待补充：${result.missing_fields.join("、")}`));
      }
      if (result.extracted_fields && Object.keys(result.extracted_fields).length) {
        const fields = Object.entries(result.extracted_fields)
          .map(([name, value]) => `${name}：${value}`).join("；");
        card.append(element("p", "manual-result-detail", `提取字段：${fields}`));
      }
      testResult.append(card);
    }
  }
}

manualForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const transcripts = Array.from(
    manualTranscriptsRoot.querySelectorAll("[data-manual-transcript]"),
  ).map((textarea) => textarea.value.trim()).filter(Boolean);
  if (!transcripts.length) {
    manualStatus.textContent = "请粘贴聊天记录。";
    return;
  }
  manualStatus.textContent = `正在拆分聊天记录（${transcripts.length} 段）并逐条处理...`;
  testResult.replaceChildren(element("div", "empty", "正在拆分并处理聊天记录..."));
  setManualFormBusy(true);
  try {
    const { groups } = await api("/api/messages", {
      method: "POST",
      body: JSON.stringify({ transcripts }),
    });
    renderManualResults(groups, transcripts);
    const allResults = groups.flatMap((group) => group.results);
    const totals = allResults.reduce((counts, result) => {
      counts[result.status] = (counts[result.status] || 0) + 1;
      return counts;
    }, {});
    const segmentCount = groups.reduce((total, group) => total + group.segments.length, 0);
    manualStatus.textContent = `拆分 ${transcripts.length} 段记录、共 ${segmentCount} 个片段：发送 ${totals.delivered || 0}，过滤 ${totals.filtered || 0}，待确认 ${totals.needs_input || 0}，跳过 ${totals.duplicate || 0}，失败 ${totals.error || 0}。`;
    manualTranscriptsRoot.querySelectorAll("[data-manual-transcript]").forEach((textarea) => {
      textarea.value = "";
    });
    document.querySelector('[data-tab="test-output"]').click();
  } catch (error) {
    manualStatus.textContent = "聊天记录拆分或处理失败。";
    testResult.replaceChildren(element("div", "manual-result-error", error.message));
    document.querySelector('[data-tab="test-output"]').click();
  } finally {
    setManualFormBusy(false);
  }
});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadQuestions() {
  const { questions } = await api("/api/questions");
  document.querySelector("#question-count").textContent = questions.length;
  questionsRoot.replaceChildren();
  if (!questions.length) {
    questionsRoot.append(element("div", "empty", "目前没有待确认事项。"));
    return;
  }
  for (const item of questions) {
    const card = element("article", "question");
    card.append(element("div", "question-meta", `${item.source} · ${item.conversation} · ${item.timestamp}`));
    card.append(element("p", "question-content", item.content));
    if (item.missing_fields.length) card.append(element("p", "status-text", `缺少信息：${item.missing_fields.join("、")}`));
    if (item.explanation) card.append(element("p", "status-text", item.explanation));
    const answerLabel = element("label", "", "人工判断");
    const choice = document.createElement("select");
    for (const [value, label] of [
      ["satisfied", "满足"],
      ["not_satisfied", "不满足"],
      ["satisfied_reason", "满足并说明理由"],
      ["not_satisfied_reason", "不满足并说明理由"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      choice.append(option);
    }
    const answer = element("textarea");
    answer.rows = 3;
    answer.placeholder = "选择带理由的判断时，请填写理由";
    choice.addEventListener("change", () => {
      answer.required = choice.value.endsWith("_reason");
    });
    answer.required = choice.value.endsWith("_reason");
    const actions = element("div", "form-actions");
    const button = element("button", "button primary", "保存处理结果");
    button.type = "button";
    const progress = element("span", "status-text question-progress");
    progress.setAttribute("role", "status");
    progress.setAttribute("aria-live", "polite");
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      progress.textContent = choice.value.endsWith("_reason")
        ? "正在保存理由记忆并应用人工判断，请稍候…"
        : choice.value === "satisfied"
          ? "正在处理判断并发送邮件，请稍候…"
          : "正在保存人工判断，请稍候…";
      try {
        await api(`/api/questions/${item.question_id}/answer`, {
          method: "POST",
          body: JSON.stringify({ answer: answer.value, choice: choice.value }),
        });
        card.remove();
        const count = document.querySelector("#question-count");
        count.textContent = Math.max(0, Number(count.textContent) - 1);
      } catch (error) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        progress.textContent = `处理失败：${error.message}。可以检查信息后重试。`;
      }
    });
    actions.append(button);
    card.append(answerLabel, choice, answer, actions, progress);
    questionsRoot.append(card);
  }
}

async function refreshActivity() {
  if (activityRefreshInProgress) return;
  activityRefreshInProgress = true;
  try {
    const snapshot = await api("/api/activity");
    document.querySelector("#question-count").textContent = snapshot.pending_count;
    document.querySelector("#filtered-count").textContent = snapshot.filtered_count;
    const previous = lastActivitySnapshot;
    lastActivitySnapshot = snapshot;
    if (!previous) return;

    const activeTab = document.querySelector(".tab.active")?.dataset.tab;
    const questionsChanged =
      snapshot.pending_count !== previous.pending_count
      || snapshot.pending_updated_at !== previous.pending_updated_at;
    const filteredChanged =
      snapshot.filtered_count !== previous.filtered_count
      || snapshot.filtered_updated_at !== previous.filtered_updated_at;
    if (activeTab === "review" && questionsChanged) await loadQuestions();
    if (activeTab === "filtered" && filteredChanged) await loadFilteredMessages();
  } catch {
    // A transient activity request failure must not disrupt the active page.
  } finally {
    activityRefreshInProgress = false;
  }
}

async function loadFilteredMessages() {
  const { messages } = await api("/api/filtered-messages");
  document.querySelector("#filtered-count").textContent = messages.length;
  filteredRoot.replaceChildren();
  if (!messages.length) {
    filteredRoot.append(element("div", "empty", "当前 profile 暂无未过期的过滤信息。"));
    return;
  }
  for (const item of messages) {
    const card = element("article", "filtered-entry");
    card.append(element(
      "div",
      "question-meta",
      `${item.profile_id} · ${item.source} · 消息时间 ${item.timestamp} · 归档于 ${item.archived_at} · 保留至 ${item.expires_at}`,
    ));
    card.append(element("p", "filtered-content", item.content));
    card.append(element("p", "status-text", `判断：${item.decision}`));
    if (item.explanation) card.append(element("p", "status-text", `判断理由：${item.explanation}`));
    if (item.evidence.length) {
      card.append(element("p", "status-text", `依据：\n${item.evidence.join("\n")}`));
    }
    const promote = element("button", "button quiet", "转为待处理");
    promote.type = "button";
    promote.addEventListener("click", async () => {
      promote.disabled = true;
      try {
        await api(`/api/filtered-messages/${item.archive_id}/promote`, {
          method: "POST",
          body: "{}",
        });
        filteredStatus.textContent = "已加入待确认事项，可在“待确认”页重新处理。";
        await Promise.all([loadFilteredMessages(), loadQuestions()]);
      } catch (error) {
        promote.disabled = false;
        filteredStatus.textContent = error.message;
      }
    });
    card.append(promote);
    filteredRoot.append(card);
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  // Keep view switching client-side and refresh durable data when its tab is opened.
  // 页面切换只更新当前面板；进入对应标签页时刷新持久化数据。
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`#panel-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "review") loadQuestions();
    if (tab.dataset.tab === "filtered") {
      filteredStatus.textContent = "";
      loadFilteredMessages().catch((error) => { filteredStatus.textContent = error.message; });
    }
    if (tab.dataset.tab === "memory") {
      loadMemories().catch((error) => { memoryStatus.textContent = error.message; });
    }
  });
});

document.querySelector("#refresh-questions").addEventListener("click", loadQuestions);
document.querySelector("#refresh-filtered").addEventListener("click", () => {
  loadFilteredMessages().catch((error) => { filteredStatus.textContent = error.message; });
});

loadSettings().catch((error) => { settingsStatus.textContent = error.message; });
loadQuestions().catch(() => {});
refreshActivity();
window.setInterval(refreshActivity, 3000);