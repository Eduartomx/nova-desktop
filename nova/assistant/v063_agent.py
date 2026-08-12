from __future__ import annotations


def install_agent_v063():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_v063_patched", False):
        return mod

    original_init = Agent.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            self.memory.configure_semantic_memory(
                self.config.get("semantic_memory", {}),
                self.config.get("ollama_host", "http://127.0.0.1:11434"),
            )
        except Exception:
            pass

    Agent.__init__ = init
    Agent._nova_v063_patched = True
    return mod
