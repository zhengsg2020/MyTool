<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

const router = useRouter();
const loading = ref(false);
const sites = ref([]);
const categories = ref([]);
const activeCategoryId = ref("");

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
  try {
    const r = await fetch("/api/site-categories");
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    categories.value = Array.isArray(data) ? data : [];
  } catch (e) {
    ElMessage.error("获取类型列表失败: " + e.message);
  }
}

function flattenCategories(nodes, path = []) {
  const out = [];
  for (const node of nodes) {
    const nextPath = [...path, node.name];
    out.push({
      id: node.id,
      name: node.name,
      pathLabel: nextPath.join(" / "),
      parentId: node.parent_id || "",
    });
    if (Array.isArray(node.children) && node.children.length) {
      out.push(...flattenCategories(node.children, nextPath));
    }
  }
  return out;
}

const flatCategories = computed(() => flattenCategories(categories.value));

const categoryPathMap = computed(() => {
  return new Map(flatCategories.value.map((x) => [x.id, x.pathLabel]));
});

function collectSubtreeIds(nodes, targetId) {
  for (const node of nodes) {
    if (node.id === targetId) {
      const ids = [];
      const walk = (cur) => {
        ids.push(cur.id);
        if (Array.isArray(cur.children)) {
          cur.children.forEach(walk);
        }
      };
      walk(node);
      return ids;
    }
    if (Array.isArray(node.children) && node.children.length) {
      const nested = collectSubtreeIds(node.children, targetId);
      if (nested.length) return nested;
    }
  }
  return [];
}

const filteredSites = computed(() => {
  if (!activeCategoryId.value) return sites.value;
  const ids = new Set(collectSubtreeIds(categories.value, activeCategoryId.value));
  if (!ids.size) return sites.value;
  return sites.value.filter((x) => ids.has(x.category_id));
});

const siteTreeData = computed(() => {
  const sitesByCategory = new Map();
  for (const site of filteredSites.value) {
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
      label: "未分类",
      children: uncategorizedSites,
    });
  }
  return tree;
});

function categoryNameById(id) {
  if (!id) return "未分类";
  return categoryPathMap.value.get(id) || "未分类";
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

onMounted(async () => {
  await Promise.all([fetchSites(), fetchCategories()]);
});
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
      <div class="filter-row">
        <el-select v-model="activeCategoryId" clearable placeholder="按类型筛选（含子类型）">
          <el-option value="" label="全部类型" />
          <el-option
            v-for="item in flatCategories"
            :key="item.id"
            :label="item.pathLabel"
            :value="item.id"
          />
        </el-select>
      </div>
      <el-tree
        :data="siteTreeData"
        node-key="id"
        default-expand-all
        :expand-on-click-node="false"
        :props="{ label: 'label', children: 'children' }"
        v-loading="loading"
      >
        <template #default="{ data }">
          <div v-if="data.type === 'category'" class="site-tree-category">
            <span>{{ data.label }}</span>
          </div>
          <div v-else class="site-tree-site">
            <span class="site-tree-main">{{ data.site.name || "-" }}</span>
            <span class="site-tree-url">{{ data.site.url }}</span>
            <span class="site-tree-cred">{{ data.site.username || "-" }}</span>
            <span class="site-tree-cred">{{ data.site.password ? "******" : "" }}</span>
            <el-space wrap>
              <el-button size="small" type="primary" plain @click="openSite(data.site.url)">访问网站</el-button>
              <el-button size="small" :disabled="!data.site.username" @click="copyText(data.site.username, '账号')">
                复制账号
              </el-button>
              <el-button size="small" :disabled="!data.site.password" @click="copyText(data.site.password, '密码')">
                复制密码
              </el-button>
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
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header {
  font-weight: 600;
}
.filter-row {
  margin-bottom: 12px;
}
.site-tree-category {
  font-weight: 600;
}
.site-tree-site {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
}
.site-tree-main {
  min-width: 120px;
}
.site-tree-url {
  min-width: 260px;
  color: #606266;
}
.site-tree-cred {
  min-width: 80px;
  color: #909399;
}
</style>
