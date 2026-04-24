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
const editingSiteId = ref("");
const editForm = reactive({
  name: "",
  url: "",
  username: "",
  password: "",
  category_id: "",
});

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
        depth: nextPath.length,
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

  const parent = categoryOptions.value.find((x) => x.value === categoryForm.parent_id);
  const newDepth = (parent?.depth || 0) + 1;
  if (newDepth > 3) {
    ElMessage.warning("最多只能创建 3 层类型");
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

function truncateText(value, maxLen) {
  const text = String(value || "");
  if (!text) return "-";
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen)}...`;
}

function startEditSite(site) {
  editingSiteId.value = site.id;
  editForm.name = site.name || "";
  editForm.url = site.url || "";
  editForm.username = site.username || "";
  editForm.password = site.password || "";
  editForm.category_id = site.category_id || "";
}

function cancelEditSite() {
  editingSiteId.value = "";
}

async function saveEditSite(site) {
  const payload = {
    name: editForm.name,
    url: editForm.url,
    username: editForm.username,
    password: editForm.password,
    category_id: editForm.category_id,
  };

  try {
    const r = await fetch(`/api/sites/${encodeURIComponent(site.id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    ElMessage.success("修改成功");
    editingSiteId.value = "";
    await fetchSites();
  } catch (e) {
    ElMessage.error("修改失败: " + e.message);
  }
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
          <div v-else class="site-tree-site" :class="{ 'is-editing': editingSiteId === data.site.id }">
            <span class="drag-handle" draggable="true" @dragstart="onSiteDragStart(data.site)" @dragend="onSiteDragEnd">
              拖动
            </span>
            <template v-if="editingSiteId === data.site.id">
              <el-input v-model="editForm.name" size="small" placeholder="名称" class="site-edit-input" />
              <el-input v-model="editForm.url" size="small" placeholder="URL" class="site-edit-input" />
              <el-input v-model="editForm.username" size="small" placeholder="账号" class="site-edit-input" />
              <el-input v-model="editForm.password" size="small" placeholder="密码" class="site-edit-input" />
            </template>
            <template v-else>
              <span class="site-tree-main" :title="data.site.name || '-'">{{ data.site.name || "-" }}</span>
              <span class="site-tree-url" :title="data.site.url">{{ truncateText(data.site.url, 30) }}</span>
              <span class="site-tree-cred" :title="data.site.username || '-'">{{ truncateText(data.site.username, 10) }}</span>
              <span class="site-tree-cred" :title="data.site.password || '-'">{{ truncateText(data.site.password, 10) }}</span>
            </template>
            <div class="site-tree-actions" :class="{ 'is-editing': editingSiteId === data.site.id }">
              <template v-if="editingSiteId === data.site.id">
                <el-select v-model="editForm.category_id" size="small" clearable placeholder="类型" class="site-edit-category">
                  <el-option
                    v-for="item in categoryOptions"
                    :key="item.value"
                    :label="item.pathLabel"
                    :value="item.value"
                  />
                </el-select>
                <el-button size="small" type="primary" @click="saveEditSite(data.site)">保存</el-button>
                <el-button size="small" @click="cancelEditSite">取消</el-button>
              </template>
              <template v-else>
                <el-button size="small" @click="startEditSite(data.site)">修改</el-button>
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
              </template>
            </div>
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
  min-height: 20px;
  padding: 3px 0;
}
.site-tree-site {
  --site-actions-width: 420px;
  width: 100%;
  display: grid;
  grid-template-columns: 44px minmax(90px, 140px) minmax(160px, 280px) minmax(70px, 110px) minmax(70px, 110px);
  align-items: center;
  justify-content: start;
  column-gap: 8px;
  min-height: 30px;
  padding: 5px calc(var(--site-actions-width) + 10px) 5px 4px;
  font-size: 13px;
  line-height: 1.35;
  transition: background-color 0.18s ease;
  overflow: hidden;
  position: relative;
}
.site-tree-site.is-editing {
  --site-actions-width: 320px;
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
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: var(--site-actions-width);
  display: grid;
  grid-template-columns: 48px 72px 72px 72px 72px 54px;
  justify-content: end;
  column-gap: 6px;
  align-items: center;
  overflow: visible;
}
.site-tree-actions :deep(.el-button) {
  margin: 0;
}
.site-tree-actions.is-editing {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: var(--site-actions-width);
  justify-content: flex-end;
  gap: 6px;
}
.site-tree-actions.is-editing :deep(.el-button) {
  width: auto;
}
.site-edit-input {
  width: 100%;
}
.site-edit-category {
  width: 160px;
}

:deep(.el-tree-node__content) {
  height: auto;
  min-height: 32px;
  align-items: center;
}

@media (max-width: 1400px) {
  .site-tree-site {
    --site-actions-width: 420px;
    grid-template-columns: 44px minmax(80px, 120px) minmax(130px, 220px) 90px 90px;
  }

  .site-tree-site.is-editing {
    --site-actions-width: 300px;
  }
}
</style>
