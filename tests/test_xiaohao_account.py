import unittest
from types import SimpleNamespace

from cultivator_xiaohao import CultivatorXiaoHao


class XiaoHaoAccountTests(unittest.TestCase):
    def test_start_miniapp_scheduler_tasks_registers_inventory_and_fishing_loops(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        registered = []
        actor.create_scheduler_task = lambda name, factory: registered.append(name)
        actor._miniapp_inventory = SimpleNamespace(run_loop=lambda: None)
        actor._miniapp_fishing = SimpleNamespace(supported=True, run_loop=lambda: None)
        actor._miniapp_beast_contract = SimpleNamespace(enabled=False, run=lambda: None)
        actor._miniapp_beast_abyss = SimpleNamespace(enabled=False, run_loop=lambda: None)
        actor._miniapp_beast_seek = SimpleNamespace(enabled=False, run_loop=lambda: None)
        actor._miniapp_daily_activities = None

        actor.start_miniapp_scheduler_tasks()

        self.assertEqual(registered, ["miniapp_inventory", "miniapp_fishing"])


if __name__ == "__main__":
    unittest.main()
