<script setup>
import { ref, computed, nextTick, onMounted } from "vue";
import { ElMessage } from "element-plus";

const projects = ref([]);
const selected = ref("");
const repoOptions = ref([]);
const selectedRepos = ref([]);
const logLines = ref([]);
const wsConnected = ref(false);
const building = ref(false);
const finishedOk = ref(null);
let socket = null;

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

function appendLog(line) {
  logLines.value.push(line);
  const p = inferPhase(line);
  if (p) phase.value = p;
  nextTick(() => {
    const el = logRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

function clearLog() {
  logLines.value = [];
  phase.value = "idle";
  finishedOk.value = null;
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
    repoOptions.value = Array.isArray(data) ? data : [];
    // 默认全选
    selectedRepos.value = [...repoOptions.value];
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
  clearLog();
  building.value = true;
  finishedOk.value = null;
  phase.value = "preparing";

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  const reposParam = encodeURIComponent(selectedRepos.value.join(","));
  const url = `${proto}//${host}/ws/build/${encodeURIComponent(
    selected.value
  )}?repos=${reposParam}`;
  socket = new WebSocket(url);
  wsConnected.value = true;

  socket.onmessage = (ev) => {
    const text = String(ev.data);
    if (text === "SUCCESS") {
      finishedOk.value = true;
      phase.value = "success";
      building.value = false;
      disconnect();
      ElMessage.success("构建与推送完成");
      return;
    }
    if (text === "FAILED") {
      finishedOk.value = false;
      if (phase.value !== "success") phase.value = "failed";
      building.value = false;
      disconnect();
      ElMessage.error("构建或推送失败");
      return;
    }
    appendLog(text);
  };

  socket.onerror = () => {
    appendLog("[ERROR] WebSocket 连接错误");
    phase.value = "failed";
    building.value = false;
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

onMounted(fetchProjects);
</script>

<template>
  <el-container class="page">
    <el-aside width="240px" class="aside">
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
        :loading="building"
        :disabled="!selected"
        @click="startBuild"
      >
        开始构建
      </el-button>
      <el-button class="btn-clear" :disabled="building" @click="clearLog">清除日志</el-button>
      <div class="meta">
        <span v-if="wsConnected" class="dot on" />{{ wsConnected ? "已连接" : "未连接" }}
      </div>
    </el-aside>
    <el-main class="main">
      <div class="toolbar">
        <el-tag :type="statusType" effect="dark" size="large">{{ statusLabel }}</el-tag>
        <el-button size="small" :disabled="building || !selected" @click="startBuild">
          重新构建
        </el-button>
      </div>
      <div ref="logRef" class="console">
        <div v-for="(line, i) in logLines" :key="i" class="line">{{ line }}</div>
        <div v-if="!logLines.length" class="placeholder">日志将显示在此处...</div>
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
.line {
  white-space: pre-wrap;
  word-break: break-all;
}
.placeholder {
  color: #585b70;
}
</style>
