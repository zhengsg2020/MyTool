<script setup>
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

const loading = ref(false);
const sites = ref([]);

const formRef = ref(null);
const form = reactive({
  name: "",
  url: "",
  username: "",
  password: "",
});

const rules = {
  url: [
    { required: true, message: "请输入网站地址", trigger: "blur" },
    { type: "url", message: "请输入合法 URL", trigger: "blur" },
  ],
  username: [{ required: true, message: "请输入账号", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
};

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

async function submitForm() {
  await formRef.value.validate();
  try {
    const r = await fetch("/api/sites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (!r.ok) throw new Error(await r.text());
    ElMessage.success("新增网站配置成功");
    form.name = "";
    form.url = "";
    form.username = "";
    form.password = "";
    await fetchSites();
  } catch (e) {
    ElMessage.error("新增失败: " + e.message);
  }
}

function openSite(url) {
  window.open(url, "_blank", "noopener");
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(`已复制${label}`);
  } catch {
    ElMessage.error(`复制${label}失败`);
  }
}

async function deleteSite(row) {
  try {
    await ElMessageBox.confirm(`确认删除网站配置：${row.url}？`, "提示", { type: "warning" });
    const r = await fetch(`/api/sites/${encodeURIComponent(row.id)}`, { method: "DELETE" });
    if (!r.ok) throw new Error(await r.text());
    ElMessage.success("删除成功");
    await fetchSites();
  } catch (e) {
    if (e !== "cancel") ElMessage.error("删除失败: " + e.message);
  }
}

onMounted(fetchSites);
</script>

<template>
  <div class="page">
    <el-card class="card" shadow="never">
      <template #header>
        <div class="header">新增网站配置</div>
      </template>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="网站名称" prop="name">
          <el-input v-model="form.name" placeholder="可选" />
        </el-form-item>
        <el-form-item label="网站地址" prop="url">
          <el-input v-model="form.url" placeholder="https://example.com" />
        </el-form-item>
        <el-form-item label="账号" prop="username">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="submitForm">保存配置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="card" shadow="never">
      <template #header>
        <div class="header">已配置网站</div>
      </template>
      <el-table :data="sites" v-loading="loading" border>
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="url" label="网站地址" min-width="260" />
        <el-table-column prop="username" label="账号" min-width="120" />
        <el-table-column label="密码" min-width="100">
          <template #default>******</template>
        </el-table-column>
        <el-table-column label="操作" min-width="340" fixed="right">
          <template #default="{ row }">
            <el-space wrap>
              <el-button size="small" type="primary" plain @click="openSite(row.url)">访问网站</el-button>
              <el-button size="small" @click="copyText(row.username, '账号')">复制账号</el-button>
              <el-button size="small" @click="copyText(row.password, '密码')">复制密码</el-button>
              <el-button size="small" type="danger" plain @click="deleteSite(row)">删除</el-button>
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
.header {
  font-weight: 600;
}
</style>
