const { ItemView, Plugin } = require("obsidian");
const VIEW = "video-extract-learning-map";
class LearningMapView extends ItemView {
  getViewType() { return VIEW; }
  getDisplayText() { return "局部问题图"; }
  async onOpen() {
    const frame = this.containerEl.children[1].createEl("iframe", { cls: "video-extract-learning-map-frame" });
    frame.setAttr("sandbox", "allow-scripts allow-same-origin");
    frame.setAttr("title", "只读局部问题图");
    frame.addEventListener("load", () => frame.contentDocument?.addEventListener("click", (event) => {
      const link = event.target.closest("a"); if (!link) return;
      event.preventDefault(); this.app.workspace.openLinkText(decodeURI(link.getAttribute("href")), "", false);
    }));
    const listing = await this.app.vault.adapter.list("learning-views/current");
    const pointerPath = listing.files.find(path => path.endsWith(".json"));
    if (!pointerPath) { this.containerEl.children[1].createEl("p", { text: "学习地图未同步：请先运行 view build。" }); return; }
    const pointer = JSON.parse(await this.app.vault.adapter.read(pointerPath));
    frame.src = this.app.vault.adapter.getResourcePath(`learning-views/generations/${pointer.generation_id}/局部问题图.html`);
  }
  async onClose() { this.containerEl.empty(); }
}
module.exports = class LearningMapPlugin extends Plugin {
  async onload() {
    this.registerView(VIEW, leaf => new LearningMapView(leaf));
    this.registerEvent(this.app.workspace.on("file-open", () => {}));
    this.addCommand({ id: "open-local-map", name: "打开只读局部问题图", callback: async () => {
      const leaf = this.app.workspace.getLeaf(true); await leaf.setViewState({ type: VIEW, active: true });
    }});
  }
  onunload() { this.app.workspace.detachLeavesOfType(VIEW); }
};
