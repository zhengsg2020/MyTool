<script setup>
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

const route = useRoute();
const router = useRouter();
const active = computed(() => (route.path.startsWith("/sites") ? "sites" : "build"));

function goBuild() {
  router.push("/build");
}

function goSites() {
  router.push("/sites");
}
</script>

<template>
  <div class="app">
    <div class="top-nav">
      <el-button :type="active === 'build' ? 'primary' : 'default'" @click="goBuild">
        构建发布
      </el-button>
      <el-button :type="active === 'sites' ? 'primary' : 'default'" @click="goSites">
        网站管理
      </el-button>
    </div>
    <router-view />
  </div>
</template>

<style scoped>
.app {
  min-height: 100vh;
}
.top-nav {
  height: 57px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid #e5e7eb;
  background: #ffffff;
}
</style>
