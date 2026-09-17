const { ItemView, Plugin } = require("obsidian");
const VIEW = "video-extract-learning-map";
const VIEW_NAMES = [["局部问题图.html", "局部问题图"], ["模块全景图.html", "模块全景图"], ["问题目录.html", "问题目录"]];

async function sha256(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

class LearningMapView extends ItemView {
  getViewType() { return VIEW; }
  getDisplayText() { return "学习导航"; }

  async onOpen() {
    const content = this.containerEl.children[1];
    const listing = await this.app.vault.adapter.list("learning-views/current");
    const pointerPaths = listing.files.filter(path => path.endsWith(".json")).sort();
    if (!pointerPaths.length) {
      content.createEl("p", { text: "学习地图未同步：请先运行 view build。" });
      return;
    }
    if (pointerPaths.length > 1) {
      content.createEl("p", { text: "存在多个学习模块，请先选择要查看的模块。" });
      const picker = content.createEl("select", { cls: "video-extract-learning-map-module" });
      const placeholder = picker.createEl("option", { text: "选择模块…" });
      placeholder.value = "";
      for (const path of pointerPaths) {
        const pointer = JSON.parse(await this.app.vault.adapter.read(path));
        const option = picker.createEl("option", { text: pointer.module_id });
        option.value = path;
      }
      picker.addEventListener("change", () => picker.value && this.renderPointer(content, picker.value));
      return;
    }
    await this.renderPointer(content, pointerPaths[0]);
  }

  async renderPointer(content, pointerPath) {
    content.empty();
    const pointer = JSON.parse(await this.app.vault.adapter.read(pointerPath));
    const validPointer = pointer.schema_version === 2
      && /^module-[A-Za-z0-9-]+$/.test(pointer.module_id || "")
      && /^view-generation-[0-9a-f]{64}$/.test(pointer.generation_id || "")
      && /^learning-commit-[0-9a-f]{64}$/.test(pointer.learning_commit_id || "")
      && /^[0-9a-f]{64}$/.test(pointer.manifest_sha256 || "");
    if (!validPointer) {
      content.createEl("p", { cls: "video-extract-learning-map-status unsynced",
        text: "学习导航指针无效；请运行 view status 后重建。" });
      return;
    }
    const generationRoot = `learning-views/generations/${pointer.generation_id}`;
    const manifestPath = `${generationRoot}/manifest.json`;
    let manifestText; let manifest;
    try {
      manifestText = await this.app.vault.adapter.read(manifestPath);
      manifest = JSON.parse(manifestText);
    } catch (_) {
      content.createEl("p", { cls: "video-extract-learning-map-status unsynced",
        text: "学习导航代缺失或无法读取；请运行 view status 后重建。" });
      return;
    }
    const validManifest = pointer.schema_version === 2
      && pointer.manifest_sha256 === await sha256(manifestText)
      && manifest.schema_version === 2
      && manifest.module_id === pointer.module_id
      && manifest.generation_id === pointer.generation_id
      && manifest.learning_commit_id === pointer.learning_commit_id;
    if (!validManifest) {
      content.createEl("p", { cls: "video-extract-learning-map-status unsynced",
        text: "学习导航代无效或已被修改；保留现有文件，请运行 view status 后重建。" });
      return;
    }

    this.pointerPath = pointerPath;
    this.statusPath = `learning-views/status/${pointer.module_id}.json`;
    this.openedGeneration = pointer.generation_id;
    let syncState = "unsynced";
    try { syncState = JSON.parse(await this.app.vault.adapter.read(this.statusPath)).sync_state; } catch (_) {}
    const publication = syncState === "current" ? "已发布（是否最新请以 view status 核对）" : "未同步";
    this.currentStatusText = `视图状态：已发布（是否最新请以 view status 核对） · 视图代：${pointer.generation_id} · 学习修订：${pointer.learning_commit_id}`;
    this.statusEl = content.createEl("p", { cls: `video-extract-learning-map-status ${syncState}`,
      text: `视图状态：${publication} · 视图代：${pointer.generation_id} · 学习修订：${pointer.learning_commit_id}` });
    const toolbar = content.createDiv({ cls: "video-extract-learning-map-toolbar" });
    const frame = content.createEl("iframe", { cls: "video-extract-learning-map-frame" });
    frame.setAttr("sandbox", "allow-scripts allow-same-origin");
    let graphPath = `${generationRoot}/局部问题图.html`;
    frame.addEventListener("load", () => frame.contentDocument?.addEventListener("click", async (event) => {
      const link = event.target.closest("a"); if (!link) return;
      const href = decodeURI(link.getAttribute("href") || "");
      if (!href || href.startsWith("#")) return;
      event.preventDefault();
      const [documentPath, sectionId] = href.split("#^");
      const document = Object.values(manifest.documents || {}).find(item =>
        item.logical_path === documentPath && item.section_id === sectionId);
      if (!document) {
        this.markInvalid("完整讲解定位不属于当前视图代；请运行 view status 后重建。");
        return;
      }
      try {
        const projected = await this.app.vault.adapter.read(`${generationRoot}/${documentPath}`);
        if (await sha256(projected) !== document.projection_sha256 || !projected.includes(`^${sectionId}`)) {
          this.markInvalid("完整讲解或稳定定位已被修改；请运行 view status 后重建。");
          return;
        }
      } catch (_) {
        this.markInvalid("完整讲解缺失；请运行 view status 后重建。");
        return;
      }
      this.app.workspace.openLinkText(href, graphPath, false);
    }));
    const show = async (name, title) => {
      const expected = manifest.views?.[name];
      if (!expected) { this.markInvalid(`${title} 未包含在当前完整视图代中。`); return; }
      graphPath = `${generationRoot}/${name}`;
      const source = await this.app.vault.adapter.read(graphPath);
      if (await sha256(source) !== expected) {
        this.markInvalid(`${title} 已被修改或损坏；请运行 view status 后重建。`);
        return;
      }
      frame.setAttr("title", `只读${title}`);
      frame.srcdoc = source;
    };
    for (const [name, title] of VIEW_NAMES) {
      const button = toolbar.createEl("button", { text: title });
      button.addEventListener("click", () => show(name, title));
    }
    await show("局部问题图.html", "局部问题图");
    this.versionTimer = window.setInterval(() => this.refreshVersion(), 2000);
  }

  markInvalid(message) {
    if (!this.statusEl) return;
    this.statusEl.setText(message);
    this.statusEl.classList.add("unsynced");
  }

  async refreshVersion() {
    if (!this.pointerPath || !this.statusPath || !this.statusEl) return;
    try {
      const current = JSON.parse(await this.app.vault.adapter.read(this.pointerPath));
      const status = JSON.parse(await this.app.vault.adapter.read(this.statusPath));
      if (status.sync_state !== "current") {
        this.markInvalid(`当前视图仍可阅读，但最新重建未同步：${this.openedGeneration}`);
      } else if (current.generation_id !== this.openedGeneration) {
        this.markInvalid(`正在查看旧版本：${this.openedGeneration} · 新视图代：${current.generation_id}。请重新打开学习导航。`);
      } else {
        this.statusEl.setText(this.currentStatusText);
        this.statusEl.classList.remove("unsynced");
        this.statusEl.classList.add("current");
      }
    } catch (_) {
      this.markInvalid(`当前视图仍可阅读，但版本状态无法刷新：${this.openedGeneration}`);
    }
  }

  async onClose() {
    if (this.versionTimer) window.clearInterval(this.versionTimer);
    this.containerEl.empty();
  }
}

module.exports = class LearningMapPlugin extends Plugin {
  async onload() {
    this.registerView(VIEW, leaf => new LearningMapView(leaf));
    this.addCommand({ id: "open-learning-navigation", name: "打开只读学习导航", callback: async () => {
      const leaf = this.app.workspace.getLeaf(true); await leaf.setViewState({ type: VIEW, active: true });
    }});
  }
  onunload() { this.app.workspace.detachLeavesOfType(VIEW); }
};
