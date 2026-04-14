<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

const router = useRouter();
const loading = ref(false);
const sites = ref([]);

const formRef = ref(null);
const form = reactive({
  name: "",
  url: "",
  username: "",
  password: "",
  category_id: "",
});
const categories = ref([]);
const categoryLoading = ref(false);
const categoryForm = reactive({
  name: "",
  parent_id: "",
});
const draggingSite = ref(null);

const rules = {
  url: [
    { required: true, message: "请输入网站地址", trigger: "blur" },
    { type: "url", message: "请输入合法 URL", trigger: "blur" },
  ],
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

async function fetchCategories() {
  categoryLoading.value = true;
  try {
    const r = await fetch("/api/site-categories");
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    categories.value = Array.isArray(data) ? data : [];
  } catch (e) {
    ElMessage.error("获取类型列表失败: " + e.message);
  } finally {
    categoryLoading.value = false;
  }
}

const categoryOptions = computed(() => {
  const walk = (nodes, path = []) => {
    const out = [];
    for (const node of nodes) {
      const nextPath = [...path, node.name];
      out.push({
        value: node.id,
        label: node.name,
        pathLabel: nextPath.join(" / "),
      });
      if (Array.isArray(node.children) && node.children.length) {
        out.push(...walk(node.children, nextPath));
      }
    }
    return out;
  };
  return walk(categories.value);
});

const siteTreeData = computed(() => {
  const sitesByCategory = new Map();
  for (const site of sites.value) {
    const key = site.category_id || "__uncategorized__";
    if (!sitesByCategory.has(key)) sitesByCategory.set(key, []);
    sitesByCategory.get(key).push(site);
  }

  const convertCategory = (node) => {
    const categoryChildren = Array.isArray(node.children) ? node.children.map(convertCategory) : [];
    const siteChildren = (sitesByCategory.get(node.id) || []).map((site) => ({
      id: `site-${site.id}`,
      type: "site",
      site,
      label: site.name || site.url,
      children: [],
    }));
    return {
      id: `category-${node.id}`,
      type: "category",
      category: node,
      label: node.name,
      children: [...categoryChildren, ...siteChildren],
    };
  };

  const tree = categories.value.map(convertCategory);
  const uncategorizedSites = (sitesByCategory.get("__uncategorized__") || []).map((site) => ({
    id: `site-${site.id}`,
    type: "site",
    site,
    label: site.name || site.url,
    children: [],
  }));
  if (uncategorizedSites.length) {
    tree.push({
      id: "category-uncategorized",
      type: "category",
      category: null,
      label: "未分类",
      children: uncategorizedSites,
    });
  }
  return tree;
});

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
    form.category_id = "";
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

async function createCategory() {
  if (!categoryForm.name.trim()) {
    ElMessage.warning("请输入类型名称");
    return;
  }
  try {
    const r = await fetch("/api/site-categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: categoryForm.name,
        parent_id: categoryForm.parent_id || null,
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    ElMessage.success("已新增类型");
    categoryForm.name = "";
    await fetchCategories();
  } catch (e) {
    ElMessage.error("新增类型失败: " + e.message);
  }
}

function onSiteDragStart(site) {
  draggingSite.value = site;
}

function onSiteDragEnd() {
  draggingSite.value = null;
}

async function assignSiteToCategory(siteId, categoryId) {
  const r = await fetch(`/api/sites/${encodeURIComponent(siteId)}/category`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category_id: categoryId || "" }),
  });
  if (!r.ok) throw new Error(await r.text());
  await fetchSites();
}

async function onCategoryNodeDrop(nodeData) {
  if (!draggingSite.value) return;
  const targetCategoryId = nodeData?.id || "";
  if (draggingSite.value.category_id === targetCategoryId) {
    draggingSite.value = null;
    return;
  }
  try {
    await assignSiteToCategory(draggingSite.value.id, targetCategoryId);
    ElMessage.success(`已将网站移动到类型：${nodeData.name}`);
  } catch (e) {
    ElMessage.error("拖拽归类失败: " + e.message);
  } finally {
    draggingSite.value = null;
  }
}

async function clearSiteCategory(site) {
  try {
    await assignSiteToCategory(site.id, "");
    ElMessage.success("已移出类型");
  } catch (e) {
    ElMessage.error("移出类型失败: " + e.message);
  }
}

async function deleteCategory(nodeData) {
  try {
    await ElMessageBox.confirm(`确认删除类型：${nodeData.name}？`, "提示", { type: "warning" });
    const r = await fetch(`/api/site-categories/${encodeURIComponent(nodeData.id)}`, {
      method: "DELETE",
    });
    if (!r.ok) throw new Error(await r.text());
    ElMessage.success("类型删除成功");
    if (form.category_id === nodeData.id) form.category_id = "";
    if (categoryForm.parent_id === nodeData.id) categoryForm.parent_id = "";
    await Promise.all([fetchCategories(), fetchSites()]);
  } catch (e) {
    if (e !== "cancel") ElMessage.error("删除类型失败: " + e.message);
  }
}

function goSites() {
  router.push("/sites");
}

onMounted(async () => {
  await Promise.all([fetchSites(), fetchCategories()]);
});
</script>

<template>
  <div class="page">
    <el-card class="card" shadow="never">
      <template #header>
        <div class="header-row">
          <div class="header">管理员 - 网站配置</div>
          <el-button size="small" @click="goSites">返回网站</el-button>
        </div>
      </template>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="网站名称" prop="name">
          <el-input v-model="form.name" placeholder="可选" />
        </el-form-item>
        <el-form-item label="网站地址" prop="url">
          <el-input v-model="form.url" placeholder="https://example.com" />
        </el-form-item>
        <el-form-item label="账号" prop="username">
          <el-input v-model="form.username" placeholder="可选" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="可选" />
        </el-form-item>
        <el-form-item label="网站类型" prop="category_id">
          <el-select v-model="form.category_id" placeholder="可选，不选则未分类" clearable>
            <el-option
              v-for="item in categoryOptions"
              :key="item.value"
              :label="item.pathLabel"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="submitForm">保存配置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="card" shadow="never">
      <template #header>
        <div class="header">网站类型与网站（层级展示）</div>
      </template>
      <el-form :inline="true" class="category-form">
        <el-form-item label="类型名称">
          <el-input v-model="categoryForm.name" placeholder="例如：正式服 / 测试服" />
        </el-form-item>
        <el-form-item label="父级类型">
          <el-select v-model="categoryForm.parent_id" placeholder="留空为根类型" clearable>
            <el-option
              v-for="item in categoryOptions"
              :key="item.value"
              :label="item.pathLabel"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="createCategory">新增类型</el-button>
        </el-form-item>
      </el-form>
      <el-tree
        :data="siteTreeData"
        node-key="id"
        default-expand-all
        :expand-on-click-node="false"
        :props="{ label: 'label', children: 'children' }"
        v-loading="loading || categoryLoading"
      >
        <template #default="{ data }">
          <div
            v-if="data.type === 'category'"
            class="site-tree-category"
            @dragover.prevent
            @drop.prevent="data.category ? onCategoryNodeDrop(data.category) : null"
          >
            <span>{{ data.label }}</span>
            <el-button
              v-if="data.category"
              size="small"
              type="danger"
              text
              @click.stop="deleteCategory(data.category)"
            >
              删除
            </el-button>
          </div>
          <div v-else class="site-tree-site">
            <span class="drag-handle" draggable="true" @dragstart="onSiteDragStart(data.site)" @dragend="onSiteDragEnd">
              拖动
            </span>
            <span class="site-tree-main">{{ data.site.name || "-" }}</span>
            <span class="site-tree-url">{{ data.site.url }}</span>
            <span class="site-tree-cred">{{ data.site.username || "-" }}</span>
            <span class="site-tree-cred">{{ data.site.password ? "******" : "" }}</span>
            <el-space class="site-tree-actions">
              <el-button size="small" type="primary" plain @click="openSite(data.site.url)">访问网站</el-button>
              <el-button size="small" :disabled="!data.site.username" @click="copyText(data.site.username, '账号')">
                复制账号
              </el-button>
              <el-button size="small" :disabled="!data.site.password" @click="copyText(data.site.password, '密码')">
                复制密码
              </el-button>
              <el-button size="small" :disabled="!data.site.category_id" @click="clearSiteCategory(data.site)">
                移出类型
              </el-button>
              <el-button size="small" type="danger" plain @click="deleteSite(data.site)">删除</el-button>
            </el-space>
          </div>
        </template>
      </el-tree>
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
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.category-form {
  margin-bottom: 12px;
}
.category-form :deep(.el-select) {
  min-width: 220px;
}
.category-form :deep(.el-select .el-input) {
  width: 100%;
}
.tree-node {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 28px;
}
.drag-handle {
  display: inline-block;
  font-size: 12px;
  color: #409eff;
  cursor: move;
  user-select: none;
}
.site-tree-category {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
.site-tree-site {
  width: 100%;
  display: grid;
  grid-template-columns: 56px 140px minmax(320px, 1fr) 120px 80px 360px;
  align-items: center;
  column-gap: 10px;
  min-height: 34px;
  padding: 0 6px;
  font-size: 13px;
  line-height: 1.45;
  transition: background-color 0.18s ease;
}
.site-tree-site:hover,
.site-tree-site:focus-within {
  background: #80deea;
}
.site-tree-site:hover .site-tree-main,
.site-tree-site:hover .site-tree-url,
.site-tree-site:hover .site-tree-cred,
.site-tree-site:focus-within .site-tree-main,
.site-tree-site:focus-within .site-tree-url,
.site-tree-site:focus-within .site-tree-cred {
  color: #11444b;
}
.site-tree-site:hover :deep(.el-button),
.site-tree-site:focus-within :deep(.el-button) {
  border-color: #0097a7;
}
.site-tree-site:hover :deep(.el-button--primary.is-plain),
.site-tree-site:focus-within :deep(.el-button--primary.is-plain) {
  background: #0097a7;
  color: #fff;
  transition: background-color 0.18s ease;
}
.site-tree-main,
.site-tree-url,
.site-tree-cred {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.site-tree-url {
  color: #606266;
}
.site-tree-cred {
  color: #909399;
}
.site-tree-actions {
  white-space: nowrap;
}
</style>
