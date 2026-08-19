import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import automation_settings
import dashboard_server
from miniapp_beast import MiniAppCircuitOpenError
from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_inventory import (
    MiniAppInventoryWorker,
    inventory_snapshot,
    normalize_inventory_sections,
    read_inventory_cache,
    read_inventory_request,
    search_inventory_caches,
    write_inventory_cache,
    write_inventory_request,
)


def section_payload(identity="主魂"):
    return {
        "inventory": {
            "account": {
                "bagTreasure": {
                    "treasures": [
                        {"itemId": f"sword_{identity}", "name": "飞剑", "active": True, "durability": 8, "maxDurability": 10},
                    ],
                    "items": [
                        {"itemId": "seed", "name": "凝血草种子", "typeLabel": "种子", "quantity": 3},
                        {"itemId": "pill", "name": "回春丹", "typeLabel": "丹药", "quantity": 4},
                    ],
                    "materials": [
                        {"name": "灵石", "quantity": 1200},
                        {"name": "空材料", "quantity": 0},
                    ],
                }
            }
        },
    }


class FakeActor:
    def __init__(self, avatars=None):
        self.avatars = list(avatars or [])
        self.is_running = True


class FakeTransport:
    def __init__(self, identities):
        self.identity_player_ids = {identity: index + 1 for index, identity in enumerate(identities)}
        self.calls = []

    async def inventory_sections(self, identity):
        self.calls.append(identity)
        return section_payload(identity)


class MiniAppInventoryTests(unittest.TestCase):
    def test_normalizes_bag_sources_and_keeps_elixirs_as_items(self):
        payload = section_payload()
        payload["inventory"]["account"]["bagTreasure"]["treasures"].append(
            {"itemId": "mirror", "name": "宝镜", "quantity": 2, "refined": True}
        )
        rows = normalize_inventory_sections(payload["inventory"])

        self.assertEqual([row["type"] for row in rows], ["法宝", "法宝", "物品", "物品", "材料"])
        self.assertEqual(
            [row["name"] for row in rows if row["type"] == "法宝"],
            sorted(["飞剑", "宝镜"], key=str.casefold),
        )
        sword = next(row for row in rows if row["name"] == "飞剑")
        self.assertEqual(sword["quantity"], 1)
        self.assertIn("已祭出", sword["detail"])
        self.assertIn("耐久 8/10", sword["detail"])
        elixir = next(row for row in rows if row["name"] == "回春丹")
        self.assertEqual(elixir["type"], "物品")
        self.assertEqual(elixir["quantity"], 4)
        self.assertEqual(elixir["detail"], "丹药")
        self.assertNotIn("空材料", [row["name"] for row in rows])

    def test_request_round_trip_and_cross_identity_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request = write_inventory_request(
                "main",
                "缘生子",
                requested_by="tester",
                request_id="request-1",
                base_dir=tmpdir,
            )
            self.assertEqual(read_inventory_request("main", tmpdir), request)

            main_cache = read_inventory_cache("main", tmpdir)
            main_cache["snapshots"]["缘生子"] = inventory_snapshot(
                "main",
                "缘生子",
                section_payload()["inventory"],
            )
            write_inventory_cache("main", main_cache, tmpdir)
            caches = {"main": read_inventory_cache("main", tmpdir)}
            results = search_inventory_caches(caches, "回春")

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["account"], "main")
            self.assertEqual(results[0]["identity"], "缘生子")
            self.assertEqual(results[0]["name"], "回春丹")

    def test_worker_refreshes_selected_and_all_available_identities(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            actor = FakeActor(["缘生子"])
            transport = FakeTransport(["主魂", "缘生子"])
            worker = MiniAppInventoryWorker(
                actor,
                transport,
                "main",
                base_dir=tmpdir,
                inter_identity_delay=0,
            )
            selected = asyncio.run(worker.process_request({
                "request_id": "selected",
                "identity": "缘生子",
                "requested_at": "2026-08-04 12:00:00",
                "requested_by": "tester",
            }))
            self.assertEqual(selected["status"], "completed")
            self.assertEqual(transport.calls, ["缘生子"])

            all_result = asyncio.run(worker.process_request({
                "request_id": "all",
                "identity": "*",
                "requested_at": "2026-08-04 12:01:00",
                "requested_by": "tester",
            }))
            cache = read_inventory_cache("main", tmpdir)
            self.assertEqual(all_result["status"], "completed")
            self.assertEqual(all_result["completed"], 2)
            self.assertEqual(list(cache["snapshots"]), ["缘生子", "主魂"])
            self.assertEqual(cache["last_completed_request_id"], "all")
            self.assertEqual(cache["snapshots"]["主魂"]["counts"], {
                "法宝": 1,
                "物品": 2,
                "材料": 1,
            })

    def test_worker_stops_refreshing_remaining_identities_when_circuit_is_open(self):
        class CircuitTransport(FakeTransport):
            async def inventory_sections(self, identity):
                self.calls.append(identity)
                raise MiniAppCircuitOpenError(900, "2026-08-19 12:00:00")

        with tempfile.TemporaryDirectory() as tmpdir:
            actor = FakeActor(["缘生子"])
            transport = CircuitTransport(["主魂", "缘生子"])
            worker = MiniAppInventoryWorker(
                actor,
                transport,
                "main",
                base_dir=tmpdir,
                inter_identity_delay=0,
            )

            result = asyncio.run(worker.process_request({
                "request_id": "circuit-open",
                "identity": "*",
                "requested_by": "tester",
            }))

            self.assertEqual(result["status"], "paused_upstream")
            self.assertEqual(transport.calls, ["主魂"])
            self.assertEqual(
                result["errors"],
                {
                    "主魂": "miniapp_circuit_open",
                    "缘生子": "miniapp_circuit_open",
                },
            )

    def test_transport_requests_only_inventory_section_for_identity(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            return {"ok": True, "account": {}}

        transport = MiniAppDwellingTransport(
            object(),
            "https://t.me/fanrenxiuxian_bot/app?startapp=fixture",
            post_json=post_json,
        )
        transport.init_data = "signed"
        transport.start_payload = {"ok": True}
        transport.identity_player_ids = {"主魂": 100, "缘生子": 200}

        result = asyncio.run(transport.inventory_sections("缘生子"))

        self.assertEqual(set(result), {"inventory"})
        self.assertEqual([call[0] for call in calls], [
            "/api/miniapp/xianxia-dwelling/section",
        ])
        self.assertEqual([call[1]["section"] for call in calls], ["inventory"])
        self.assertTrue(all(call[1]["playerId"] == 200 for call in calls))

    def test_dashboard_payload_and_refresh_endpoint_use_inventory_cache_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state_sub.json"
            state_path.write_text(json.dumps({
                "avatar_dao_names_by_player_id": {
                    automation_settings.SUB_YINLUO_PLAYER_ID: "竹和生",
                },
            }), encoding="utf-8")
            with (
                patch.object(automation_settings, "SUB_STATE_FILE", state_path),
                patch.object(dashboard_server, "CONFIG_DIR", tmpdir),
            ):
                cache = read_inventory_cache("sub", tmpdir)
                payload = section_payload()
                cache["snapshots"]["厚土"] = inventory_snapshot(
                    "sub",
                    "厚土",
                    payload["inventory"],
                )
                cache["snapshots"]["缘生子"] = inventory_snapshot(
                    "sub",
                    "缘生子",
                    payload["inventory"],
                )
                write_inventory_cache("sub", cache, tmpdir)

                dashboard = dashboard_server.miniapp_inventory_dashboard_payload(query="灵石")
                response = dashboard_server.refresh_miniapp_inventory(
                    {"account": "sub", "identity": "厚土"},
                    username="wg",
                )

                self.assertTrue(dashboard["ok"])
                self.assertEqual(dashboard["summary"]["snapshot_count"], 2)
                self.assertEqual(dashboard["summary"]["match_count"], 2)
                self.assertEqual(dashboard["summary"]["match_quantity"], 2400)
                self.assertIn(
                    "竹和生",
                    {row["identity"] for row in dashboard["search_results"]},
                )
                self.assertNotIn(
                    "缘生子",
                    {row["identity"] for row in dashboard["search_results"]},
                )
                self.assertEqual(
                    dashboard["accounts"][1]["snapshots"]["竹和生"]["identity"],
                    "竹和生",
                )
                self.assertEqual(dashboard["search_results"][0]["identity"], "厚土")
                total_lingshi = next(row for row in dashboard["inventory_totals"] if row["name"] == "灵石")
                self.assertEqual(total_lingshi["quantity"], 2400)
                self.assertEqual(total_lingshi["source_count"], 2)

                moved = dashboard_server.update_miniapp_inventory_non_tradable(
                    {"action": "add", "items": ["灵石", "灵石", "回春丹"]},
                    username="wg",
                )
                self.assertTrue(moved["success"])
                self.assertEqual(moved["items"], ["回春丹", "灵石"])
                self.assertEqual(
                    dashboard_server.miniapp_inventory_dashboard_payload()["non_tradable_items"],
                    ["回春丹", "灵石"],
                )
                removed = dashboard_server.update_miniapp_inventory_non_tradable(
                    {"action": "remove", "items": ["灵石"]},
                    username="wg",
                )
                self.assertTrue(removed["success"])
                self.assertEqual(removed["items"], ["回春丹"])
                self.assertTrue(response["success"])
                self.assertEqual(read_inventory_request("sub", tmpdir)["identity"], "厚土")
                migrated_request = write_inventory_request(
                    "sub", "缘生子", requested_by="tester", base_dir=tmpdir
                )
                self.assertEqual(migrated_request["identity"], "竹和生")
                self.assertEqual(read_inventory_request("sub", tmpdir)["identity"], "竹和生")

    def test_sub_inventory_follows_dao_name_change_without_module_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state_sub.json"
            state_path.write_text(json.dumps({
                "avatar_dao_names_by_player_id": {
                    automation_settings.SUB_YINLUO_PLAYER_ID: "竹和生",
                },
            }), encoding="utf-8")
            with (
                patch.object(automation_settings, "SUB_STATE_FILE", state_path),
                patch.object(dashboard_server, "CONFIG_DIR", tmpdir),
            ):
                cache = read_inventory_cache("sub", tmpdir)
                cache["snapshots"]["竹和生"] = inventory_snapshot(
                    "sub",
                    "竹和生",
                    section_payload()["inventory"],
                )
                write_inventory_cache("sub", cache, tmpdir)

                state_path.write_text(json.dumps({
                    "avatar_dao_names_by_player_id": {
                        automation_settings.SUB_YINLUO_PLAYER_ID: "松风子",
                    },
                    "avatar_dao_name_aliases": {
                        "缘生子": "松风子",
                        "竹和生": "松风子",
                    },
                }), encoding="utf-8")

                dashboard = dashboard_server.miniapp_inventory_dashboard_payload(query="灵石")
                sub_account = next(
                    row for row in dashboard["accounts"] if row["account"] == "sub"
                )
                self.assertIn("松风子", sub_account["identities"])
                self.assertNotIn("竹和生", sub_account["identities"])
                self.assertEqual(sub_account["snapshots"]["松风子"]["identity"], "松风子")
                self.assertEqual(dashboard["search_results"][0]["identity"], "松风子")

                transport = FakeTransport(["主魂", "松风子"])
                worker = MiniAppInventoryWorker(
                    FakeActor(["松风子"]),
                    transport,
                    "sub",
                    base_dir=tmpdir,
                    inter_identity_delay=0,
                )
                result = asyncio.run(worker.process_request({
                    "request_id": "renamed-selected",
                    "identity": "竹和生",
                    "requested_by": "tester",
                }))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(transport.calls, ["松风子"])

                response = dashboard_server.refresh_miniapp_inventory(
                    {"account": "sub", "identity": "竹和生"},
                    username="wg",
                )
                self.assertTrue(response["success"])
                self.assertEqual(response["request"]["identity"], "松风子")
                self.assertEqual(read_inventory_request("sub", tmpdir)["identity"], "松风子")


if __name__ == "__main__":
    unittest.main()
