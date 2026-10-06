const profilesRoot = document.querySelector("#profiles");
const profilesStatus = document.querySelector("#profiles-status");
const createForm = document.querySelector("#create-profile-form");
const createStatus = document.querySelector("#create-status");
const createButton = document.querySelector("#create-profile-button");
const launcherIdleTimeoutMs = 30 * 60 * 1000;
const activityHeartbeatIntervalMs = 10 * 1000;
let lastActivityHeartbeat = 0;
let launcherIdleTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function recordLauncherActivity() {
  clearTimeout(launcherIdleTimer);
  launcherIdleTimer = setTimeout(
    () => window.location.replace("about:blank"),
    launcherIdleTimeoutMs,
  );
  const now = Date.now();
  if (now - lastActivityHeartbeat < activityHeartbeatIntervalMs) return;
  lastActivityHeartbeat = now;
  void api("/api/heartbeat", { method: "POST", body: JSON.stringify({}) }).catch(() => {});
}

for (const eventName of ["pointerdown", "keydown", "input", "change", "scroll"]) {
  document.addEventListener(eventName, recordLauncherActivity, { passive: true });
}
recordLauncherActivity();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function appendAgentLink(container, url, label = "打开 Agent") {
  const link = element("a", "", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  container.append(link);
}

async function launchProfile(profile) {
  try {
    const result = await api("/api/profiles/launch", {
      method: "POST",
      body: JSON.stringify({ config_name: profile.config_name }),
    });
    profile.status.textContent = result.running ? "Agent 正在运行。" : "Agent 已启动。";
    profile.actions.replaceChildren();
    appendAgentLink(profile.actions, result.url);
    const remove = element("button", "button danger", "删除配置");
    remove.type = "button";
    remove.addEventListener("click", () => deleteProfile(profile.config_name));
    profile.actions.append(remove);
  } catch (error) {
    profile.status.textContent = `启动失败：${error.message}`;
  }
}

async function deleteProfile(configName) {
  const confirmed = window.confirm(
    `删除 ${configName}？此操作会同时删除该 profile 的记忆、待确认事项、过滤归档、游标和去重数据，且不可恢复。`,
  );
  if (!confirmed) return;
  profilesStatus.textContent = "正在关闭 Agent 并删除 profile 数据...";
  try {
    await api(`/api/profiles/${encodeURIComponent(configName)}`, { method: "DELETE" });
    profilesStatus.textContent = `${configName} 及其 profile 数据已删除。`;
    await loadProfiles();
  } catch (error) {
    profilesStatus.textContent = `删除失败：${error.message}`;
  }
}

async function loadProfiles() {
  profilesRoot.replaceChildren(element("div", "empty", "正在读取配置…"));
  try {
    const { profiles } = await api("/api/profiles");
    profilesRoot.replaceChildren();
    if (!profiles.length) {
      profilesRoot.append(element("div", "empty", "尚无配置，请创建一个 profile。"));
      return;
    }
    for (const item of profiles) {
      const card = element("article", `launcher-profile${item.valid ? "" : " invalid"}`);
      const heading = element("div", "launcher-profile-heading");
      heading.append(element("strong", "", item.profile_id || item.config_name));
      heading.append(element("span", "launcher-profile-meta", item.config_name));
      const meta = item.valid
        ? `${item.message_type} · ${item.llm_model} · ${item.mail_configured ? "邮箱已配置" : "邮箱信息不完整"}`
        : `配置不可读取：${item.error || "未知错误"}`;
      card.append(heading, element("div", "launcher-profile-meta", meta));
      const status = element(
        "div",
        "status-text",
        item.running ? "Agent 正在运行。" : item.valid ? "Agent 尚未启动。" : "此配置无法启动。",
      );
      const actions = element("div", "launcher-profile-actions");
      const profile = { config_name: item.config_name, status, actions };
      if (item.valid) {
        if (item.running && item.url) appendAgentLink(actions, item.url);
        else {
          const launch = element("button", "button primary", "启动 Agent");
          launch.type = "button";
          launch.addEventListener("click", () => launchProfile(profile));
          actions.append(launch);
        }
      }
      if (item.valid) {
        const remove = element("button", "button danger", "删除配置");
        remove.type = "button";
        remove.addEventListener("click", () => deleteProfile(item.config_name));
        actions.append(remove);
      }
      card.append(status, actions);
      profilesRoot.append(card);
    }
  } catch (error) {
    profilesRoot.replaceChildren(element("div", "manual-result-error", error.message));
  }
}

document.querySelector("#refresh-profiles").addEventListener("click", loadProfiles);

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(createForm).entries());
  for (const name of [
    "port",
    "mail_imap_port",
    "mail_smtp_port",
    "poll_interval_seconds",
    "dedup_retention_days",
    "filtered_retention_days",
  ]) values[name] = Number(values[name]);
  values.mail_ingestion_enabled = createForm.elements.namedItem("mail_ingestion_enabled").checked;
  createButton.disabled = true;
  createStatus.textContent = "正在验证、保存配置和凭据并启动 Agent...";
  try {
    const result = await api("/api/profiles/create", {
      method: "POST",
      body: JSON.stringify(values),
    });
    createForm.reset();
    createForm.elements.namedItem("message_type").value = "招聘信息";
    createForm.elements.namedItem("llm_model").value = "gpt-4o-mini";
    createForm.elements.namedItem("mail_folder").value = "INBOX";
    createForm.elements.namedItem("poll_interval_seconds").value = "60";
    createStatus.replaceChildren(element("span", "status-text", `${result.config_name} 已创建。`));
    appendAgentLink(createStatus, result.url);
    await loadProfiles();
  } catch (error) {
    createStatus.textContent = `创建失败：${error.message}`;
  } finally {
    createButton.disabled = false;
  }
});

loadProfiles();