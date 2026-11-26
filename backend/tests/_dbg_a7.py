
from app.services import agent_tools
from tests.agent_runtime_fixtures import fake_tool
from app.services import execution_manager


class MP:
    def setitem(self, d, k, v):
        d[k] = v


fake_tool(MP(), "fake_a7_dbg", returns={"ok": 1}, spec_overrides={"requires_approval": True})
spec = execution_manager._get_tool_spec("fake_a7_dbg")
print("spec requires_approval =", spec.get("requires_approval"))
print("in registry:", "fake_a7_dbg" in agent_tools.TOOL_REGISTRY)
