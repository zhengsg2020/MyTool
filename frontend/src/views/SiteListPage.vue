<script setup>
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

const router = useRouter();
const loading = ref(false);
const sites = ref([]);

async function fetchSites() {
  loading.value = true;
  try {
    const r = await fetch("/api/sites");
    if (!r.ok) throw new Error(await r.text());
    sites.value = await r.json();
  } catch (e) {
    ElMessage.error("获取网站配置失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

function openSite(url) {
  window.open(url, "_blank", "noopener");
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text || "");
    ElMessage.success(`已复制${label}`);
  } catch {
    ElMessage.error(`复制${label}失败`);
  }
}

function goAdmin() {
  router.push("/admin");
}

onMounted(fetchSites);
</script>

<template>
  <div class="page">
    <el-card class="card" shadow="never">
      <template #header>
        <div class="header-row">
          <div class="header">已配置网站</div>
          <el-button type="primary" plain size="small" @click="goAdmin">管理员</el-button>
        </div>
      </template>
      <el-table :data="sites" v-loading="loading" border>
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="url" label="网站地址" min-width="260" />
        <el-table-column prop="username" label="账号" min-width="120" />
        <el-table-column label="密码" min-width="100">
          <template #default>******</template>
        </el-table-column>
        <el-table-column label="操作" min-width="280" fixed="right">
          <template #default="{ row }">
            <el-space wrap>
              <el-button size="small" type="primary" plain @click="openSite(row.url)">访问网站</el-button>
              <el-button size="small" @click="copyText(row.username, '账号')">复制账号</el-button>
              <el-button size="small" @click="copyText(row.password, '密码')">复制密码</el-button>
            </el-space>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.page {
  min-height: calc(100vh - 57px);
  padding: 16px;
  background: #f5f7fa;
}
.card {
  margin-bottom: 16px;
}
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header {
  font-weight: 600;
}
</style>
