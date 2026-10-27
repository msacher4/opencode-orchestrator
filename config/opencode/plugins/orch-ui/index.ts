import { Plugin } from "@opencode/plugin"

// Côté serveur : rien à faire, l'interface vit dans ./tui (TUI uniquement).
export default Plugin.define({
  id: "orch-ui.server",
  setup() {},
})
