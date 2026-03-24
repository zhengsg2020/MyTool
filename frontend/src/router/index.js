import { createRouter, createWebHistory } from "vue-router";
import BuildPage from "../views/BuildPage.vue";
import SiteListPage from "../views/SiteListPage.vue";
import SiteManagePage from "../views/SiteManagePage.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/build" },
    { path: "/build", component: BuildPage },
    { path: "/sites", component: SiteListPage },
    { path: "/admin", component: SiteManagePage },
  ],
});

export default router;
