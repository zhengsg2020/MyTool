<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";

const projects = ref([]);
const selected = ref("");
const repoOptions = ref([]);
const selectedRepos = ref([]);
const logLines = ref([]);
const buildHistory = ref([]);
/** 根配置 proxy 列表，下拉展示完整 URL */
const proxyOptions = ref([]);
const selectedPushProxyIndex = ref(null);
const selectedBuildProxyIndex = ref(null);
/** 页面勾选：推送阿里云时是否使用代理 */
const usePushProxy = ref(false);
/**
 * docker build 时是否使用代理（与 config 中 proxy 列表配合）。
 * 默认开启：Dockerfile 常见 # syntax=docker/dockerfile:1 / FROM docker.io 需访问外网；
 * 服务进程环境往往没有 shell 里 export 的代理，不勾选易与「本机手动能编、工具不能编」不一致。
 */
const useBuildProxy = ref(true);
const showHistory = ref(false);
const wsConnected = ref(false);
const building = ref(false);
const remoteRunning = ref(false);
const finishedOk = ref(null);
let socket = null;
let statusTimer = null;

const phase = ref("idle");

const statusLabel = computed(() => {
  if (phase.value === "preparing") return "准备中";
  if (phase.value === "compiling") return "正在编译";
  if (phase.value === "tagging") return "生成版本";
  if (phase.value === "building") return "正在打包";
  if (phase.value === "pushing") return "正在推送";
  if (phase.value === "success") return "推送成功";
  if (phase.value === "failed") return "失败";
  return "就绪";
});

const statusType = computed(() => {
  if (phase.value === "success") return "success";
  if (phase.value === "failed") return "danger";
  if (["preparing", "compiling", "tagging", "building", "pushing"].includes(phase.value)) {
    return "warning";
  }
  return "info";
});

function inferPhase(line) {
  if (line.includes("[状态] 准备中")) return "preparing";
  if (line.includes("[状态] 正在编译")) return "compiling";
  if (line.includes("[状态] 正在生成版本号")) return "tagging";
  if (line.includes("[状态] 正在打包镜像")) return "building";
  if (line.includes("[状态] 正在推送镜像")) return "pushing";
  if (line.includes("[状态] 推送成功")) return "success";
  if (line.startsWith("[ERROR]")) return "failed";
  return null;
}

const logRef = ref(null);
const taskRunning = computed(() => building.value || remoteRunning.value);

function appendLog(line) {
  logLines.value.push(line);
  const p = inferPhase(line);
  if (p) phase.value = p;
  scrollLogToBottom();
}

function scrollLogToBottom() {
  nextTick(() => {
    const el = logRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

async function clearLog() {
  try {
    const r = await fetch("/api/build/log", { method: "DELETE" });
    if (!r.ok) throw new Error(await r.text());
    logLines.value = [];
    phase.value = "idle";
    finishedOk.value = null;
    ElMessage.success("已清除构建日志文件");
  } catch (e) {
    ElMessage.error("清除日志失败: " + e.message);
  }
}

function proxySelectLabel(opt) {
  return opt?.url ? String(opt.url) : "";
}

async function fetchProxyOptions() {
  try {
    const r = await fetch("/api/build/proxy-options");
    if (!r.ok) {
      proxyOptions.value = [];
      return;
    }
    const data = await r.json();
    proxyOptions.value = Array.isArray(data) ? data : [];
    if (proxyOptions.value.length) {
      if (selectedPushProxyIndex.value === null) {
        selectedPushProxyIndex.value = proxyOptions.value[0].list_index;
      }
      if (selectedBuildProxyIndex.value === null) {
        selectedBuildProxyIndex.value = proxyOptions.value[0].list_index;
      }
    }
  } catch {
    proxyOptions.value = [];
  }
}

async function fetchProjects() {
  try {
    const r = await fetch("/api/projects");
    if (!r.ok) throw new Error(await r.text());
    projects.value = await r.json();
    if (projects.value.length && !selected.value) {
      selected.value = projects.value[0];
      await fetchReposForSelected();
    }
  } catch (e) {
    ElMessage.error("获取项目列表失败: " + e.message);
  }
}

async function fetchSavedLog() {
  try {
    const r = await fetch("/api/build/log?limit=5000");
    if (!r.ok) {
      // 接口异常时保留当前日志，避免“页面把日志清空”的观感
      return;
    }
    const data = await r.json();
    if (Array.isArray(data)) {
      logLines.value = data;
      scrollLogToBottom();
    }
  } catch (e) {
    // 网络异常时保留当前日志
  }
}

async function fetchBuildHistory() {
  try {
    const r = await fetch("/api/build/history?limit=200");
    if (!r.ok) {
      // 接口异常时保留当前记录，避免误清空
      return;
    }
    const data = await r.json();
    if (Array.isArray(data)) {
      buildHistory.value = data;
    }
  } catch (e) {
    // 网络异常时保留当前记录
  }
}

async function fetchBuildStatus() {
  try {
    const r = await fetch("/api/build/status");
    if (!r.ok) return;
    const data = await r.json();
    const running = !!(data && data.running);
    remoteRunning.value = running;
    // 刷新页面后也能感知已有任务，允许“终止构建”
    if (running && !wsConnected.value) {
      building.value = true;
      if (phase.value === "idle") phase.value = "preparing";
    }
    if (!running && !wsConnected.value) {
      building.value = false;
    }
  } catch {
    // 忽略网络波动
  }
}

async function fetchReposForSelected() {
  repoOptions.value = [];
  selectedRepos.value = [];
  if (!selected.value) return;
  try {
    const r = await fetch(
      `/api/projects/${encodeURIComponent(selected.value)}/repositories`
    );
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    if (!Array.isArray(data)) {
      repoOptions.value = [];
    } else {
      repoOptions.value = data
        .map((row) => {
          if (typeof row === "string") return row;
          if (row && typeof row === "object" && typeof row.key === "string") {
            return row.key;
          }
          return null;
        })
        .filter(Boolean);
    }
    // 默认不勾选，需用户手动选择
    selectedRepos.value = [];
  } catch (e) {
    ElMessage.error("获取仓库列表失败: " + e.message);
  }
}

function disconnect() {
  if (socket) {
    socket.close();
    socket = null;
  }
  wsConnected.value = false;
}

function startBuild() {
  if (!selected.value) {
    ElMessage.warning("请先选择项目");
    return;
  }
  if (!selectedRepos.value.length) {
    ElMessage.warning("请至少勾选一个仓库");
    return;
  }
  disconnect();
  building.value = true;
  finishedOk.value = null;
  phase.value = "preparing";
  appendLog(`[信息] 开始新任务 — 项目: ${selected.value}`);

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  const reposParam = encodeURIComponent(selectedRepos.value.join(","));
  let url = `${proto}//${host}/ws/build/${encodeURIComponent(
    selected.value
  )}?repos=${reposParam}`;
  url += `&use_push_proxy=${usePushProxy.value ? "1" : "0"}`;
  url += `&use_build_proxy=${useBuildProxy.value ? "1" : "0"}`;
  if (
    usePushProxy.value &&
    proxyOptions.value.length > 0 &&
    selectedPushProxyIndex.value !== null &&
    selectedPushProxyIndex.value !== undefined &&
    selectedPushProxyIndex.value !== ""
  ) {
    url += `&push_proxy_index=${encodeURIComponent(String(selectedPushProxyIndex.value))}`;
  }
  if (
    useBuildProxy.value &&
    proxyOptions.value.length > 0 &&
    selectedBuildProxyIndex.value !== null &&
    selectedBuildProxyIndex.value !== undefined &&
    selectedBuildProxyIndex.value !== ""
  ) {
    url += `&build_proxy_index=${encodeURIComponent(String(selectedBuildProxyIndex.value))}`;
  }
  socket = new WebSocket(url);

  socket.onopen = () => {
    wsConnected.value = true;
    remoteRunning.value = true;
  };

  socket.onmessage = (ev) => {
    const text = String(ev.data);
    if (text === "SUCCESS") {
      finishedOk.value = true;
      phase.value = "success";
      building.value = false;
      remoteRunning.value = false;
      disconnect();
      ElMessage.success("构建与推送完成");
      fetchBuildHistory();
      fetchSavedLog();
      return;
    }
    if (text === "FAILED") {
      finishedOk.value = false;
      if (phase.value !== "success") phase.value = "failed";
      building.value = false;
      remoteRunning.value = false;
      disconnect();
      ElMessage.error("构建或推送失败");
      fetchBuildHistory();
      fetchSavedLog();
      return;
    }
    appendLog(text);
  };

  socket.onerror = () => {
    appendLog("[ERROR] WebSocket 连接错误");
    phase.value = "failed";
    building.value = false;
    remoteRunning.value = false;
    finishedOk.value = false;
  };

  socket.onclose = () => {
    wsConnected.value = false;
    if (building.value) {
      building.value = false;
      if (finishedOk.value === null) {
        appendLog("[WARN] 连接已断开");
        phase.value = "failed";
      }
    }
  };
}

async function cancelBuild() {
  if (!taskRunning.value) return;
  try {
    const r = await fetch("/api/build/cancel", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    appendLog("[WARN] 已请求终止构建，正在停止...");
    ElMessage.warning("已发送终止请求");
  } catch (e) {
    ElMessage.error("终止构建失败: " + e.message);
  }
}

onMounted(async () => {
  await Promise.all([
    fetchProjects(),
    fetchProxyOptions(),
    fetchSavedLog(),
    fetchBuildHistory(),
    fetchBuildStatus(),
  ]);
  statusTimer = window.setInterval(fetchBuildStatus, 3000);
});

onUnmounted(() => {
  if (statusTimer) {
    window.clearInterval(statusTimer);
    statusTimer = null;
  }
});
</script>

<template>
  <el-container class="page">
    <el-aside width="288px" class="aside">
      <div class="aside-title">项目</div>
      <el-select
        v-model="selected"
        placeholder="选择项目"
        class="project-select"
        filterable
        :disabled="building"
        @change="fetchReposForSelected"
      >
        <el-option v-for="p in projects" :key="p" :label="p" :value="p" />
      </el-select>
      <div class="aside-subtitle">上传到仓库</div>
      <el-checkbox-group
        v-model="selectedRepos"
        class="repo-group"
        :disabled="building || !selected"
      >
        <el-checkbox
          v-for="r in repoOptions"
          :key="r"
          :label="r"
          class="repo-checkbox"
        >
          {{ r }}
        </el-checkbox>
      </el-checkbox-group>
      <el-button
        type="primary"
        class="btn-build"
        :loading="taskRunning"
        :disabled="!selected || taskRunning"
        @click="startBuild"
      >
        开始构建
      </el-button>
      <el-button class="btn-clear" :disabled="building" @click="clearLog">清除日志</el-button>
      <div class="aside-subtitle proxy-section-title">构建代理</div>
      <el-checkbox v-model="useBuildProxy" class="proxy-toggle" :disabled="building">
        docker build 时使用代理
      </el-checkbox>
      <el-select
        v-if="proxyOptions.length > 0"
        v-model="selectedBuildProxyIndex"
        class="project-select proxy-select"
        placeholder="选择构建代理 URL（完整地址）"
        filterable
        :disabled="building || !useBuildProxy"
      >
        <el-option
          v-for="opt in proxyOptions"
          :key="`build-${opt.list_index}-${opt.url}`"
          :label="proxySelectLabel(opt)"
          :value="opt.list_index"
        />
      </el-select>
      <p v-if="proxyOptions.length > 0" class="proxy-hint-bottom">
        仅影响 <strong>docker build</strong> 取基础镜像与网络访问，不影响推送阶段。
      </p>
      <div class="aside-subtitle proxy-section-title">推送代理</div>
      <el-checkbox v-model="usePushProxy" class="proxy-toggle" :disabled="building">
        推送阿里云时使用代理
      </el-checkbox>
      <el-select
        v-if="proxyOptions.length > 0"
        v-model="selectedPushProxyIndex"
        class="project-select proxy-select"
        placeholder="选择代理 URL（完整地址）"
        filterable
        :disabled="building || !usePushProxy"
      >
        <el-option
          v-for="opt in proxyOptions"
          :key="`push-${opt.list_index}-${opt.url}`"
          :label="proxySelectLabel(opt)"
          :value="opt.list_index"
        />
      </el-select>
      <div v-else class="proxy-empty">
        未配置根级
        <code class="proxy-code">proxy</code>
        数组时无列表。请在
        <code class="proxy-code">config/build/config.json</code>
        中配置 URL 列表后重建前端并重启服务。
      </div>
      <p v-if="proxyOptions.length > 0" class="proxy-hint-bottom">
        勾选「使用代理」后，本次任务中所有<strong>阿里云</strong>推送会走所选 URL；内网仓库不受影响。
      </p>
      <div class="meta">
        <span v-if="wsConnected" class="dot on" />{{ wsConnected ? "已连接" : "未连接" }}
      </div>
    </el-aside>
    <el-main class="main">
      <div class="toolbar">
        <el-tag :type="statusType" effect="dark" size="large">{{ statusLabel }}</el-tag>
        <el-button size="small" :disabled="taskRunning || !selected" @click="startBuild">
          重新构建
        </el-button>
        <el-button size="small" type="danger" :disabled="!taskRunning" @click="cancelBuild">
          终止构建
        </el-button>
        <el-button size="small" @click="showHistory = !showHistory">
          {{ showHistory ? "隐藏构建记录" : "构建记录" }}
        </el-button>
      </div>
      <div ref="logRef" class="console">
        <div v-for="(line, i) in logLines" :key="i" class="line">{{ line }}</div>
        <div v-if="!logLines.length" class="placeholder">日志将显示在此处...</div>
      </div>
      <div v-if="showHistory" class="history">
        <div class="history-title">构建记录</div>
        <el-table :data="buildHistory" size="small" height="220">
          <el-table-column prop="time" label="时间" min-width="160" />
          <el-table-column prop="ip" label="IP" min-width="120" />
          <el-table-column prop="image" label="镜像名" min-width="260" show-overflow-tooltip />
          <el-table-column prop="repository" label="仓库名" min-width="120" />
        </el-table>
      </div>
    </el-main>
  </el-container>
</template>

<style scoped>
.page {
  height: calc(100vh - 57px);
  margin: 0;
}
.aside {
  background: #1e1e2e;
  color: #cdd6f4;
  padding: 16px;
  border-right: 1px solid #313244;
}
.aside-title {
  font-weight: 600;
  margin-bottom: 12px;
  font-size: 14px;
}
.aside-subtitle {
  font-weight: 500;
  margin: 12px 0 8px;
  font-size: 13px;
}
.project-select {
  width: 100%;
  margin-bottom: 12px;
}
.repo-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}
.repo-checkbox {
  margin-left: 0;
}
.proxy-section-title {
  margin-top: 4px;
}
.proxy-toggle {
  display: flex;
  width: 100%;
  margin-bottom: 8px;
}
.proxy-toggle :deep(.el-checkbox__label) {
  white-space: normal;
  line-height: 1.35;
  font-size: 12px;
  color: #cdd6f4;
}
.proxy-hint-bottom {
  margin: 0 0 4px;
  font-size: 11px;
  line-height: 1.45;
  color: #a6adc8;
}
.proxy-empty {
  margin-bottom: 10px;
  padding: 10px;
  font-size: 11px;
  line-height: 1.5;
  color: #bac2de;
  background: #181825;
  border: 1px dashed #45475a;
  border-radius: 6px;
}
.proxy-empty strong,
.proxy-hint-bottom strong {
  color: #f9e2af;
  font-weight: 600;
}
.proxy-code {
  font-family: ui-monospace, "Consolas", monospace;
  font-size: 10px;
  color: #f9e2af;
}
.proxy-select {
  margin-bottom: 12px;
}
.btn-build {
  width: 100%;
  margin-bottom: 8px;
  margin-left: 0;
}
.btn-clear {
  width: 100%;
  margin-left: 0;
}
.meta {
  margin-top: 16px;
  font-size: 12px;
  color: #a6adc8;
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  background: #585b70;
  vertical-align: middle;
}
.dot.on {
  background: #a6e3a1;
}
.main {
  padding: 0;
  display: flex;
  flex-direction: column;
  background: #11111b;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid #313244;
  background: #181825;
}
.console {
  flex: 1;
  overflow: auto;
  padding: 12px 16px;
  font-family: ui-monospace, "Cascadia Code", "Consolas", monospace;
  font-size: 13px;
  line-height: 1.5;
  color: #a6e3a1;
  background: #0d0d12;
}
.history {
  border-top: 1px solid #313244;
  background: #11111b;
  padding: 10px 12px 12px;
}
.history-title {
  color: #cdd6f4;
  font-size: 13px;
  margin-bottom: 8px;
}
.line {
  white-space: pre-wrap;
  word-break: break-all;
}
.placeholder {
  color: #585b70;
}
</style>
