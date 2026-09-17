const { ItemView, Plugin } = require("obsidian");
const VIEW = "video-extract-learning-map";
class LearningMapView extends ItemView {
  getViewType() { return VIEW; }
  getDisplayText() { return "局部问题图"; }
  async onOpen() {
    const content = this.containerEl.children[1];
    const listing = await this.app.vault.adapter.list("learning-views/current");
    const pointerPath = listing.files.find(path => path.endsWith(".json"));
    if (!pointerPath) { content.createEl("p", { text: "学习地图未同步：请先运行 view build。" }); return; }
    const pointer = JSON.parse(await this.app.vault.adapter.read(pointerPath));
    const statusPath = `learning-views/status/${pointer.module_id}.json`;
    let syncState = "unsynced";
    try { syncState = JSON.parse(await this.app.vault.adapter.read(statusPath)).sync_state; } catch (_) {}
    content.createEl("p", { cls: `video-extract-learning-map-status ${syncState}`,
      text: `同步状态：${syncState === "current" ? "已同步" : "未同步"} · 视图代：${pointer.generation_id} · 学习修订：${pointer.learning_commit_id}` });
    const graphPath = `learning-views/generations/${pointer.generation_id}/局部问题图.html`;
    const frame = content.createEl("iframe", { cls: "video-extract-learning-map-frame" });
    frame.setAttr("sandbox", "allow-scripts allow-same-origin");
    frame.setAttr("title", "只读局部问题图");
    frame.addEventListener("load", () => frame.contentDocument?.addEventListener("click", (event) => {
      const link = event.target.closest("a"); if (!link) return;
      const href = decodeURI(link.getAttribute("href") || "");
      if (!href || href.startsWith("#")) return;
      event.preventDefault(); this.app.workspace.openLinkText(href, graphPath, false);
    }));
    frame.src = this.app.vault.adapter.getResourcePath(graphPath);
  }
  async onClose() { this.containerEl.empty(); }
}
module.exports = class LearningMapPlugin extends Plugin {
  async onload() {
    this.registerView(VIEW, leaf => new LearningMapView(leaf));
    this.addCommand({ id: "open-local-map", name: "打开只读局部问题图", callback: async () => {
      const leaf = this.app.workspace.getLeaf(true); await leaf.setViewState({ type: VIEW, active: true });
    }});
  }
  onunload() { this.app.workspace.detachLeavesOfType(VIEW); }
};
