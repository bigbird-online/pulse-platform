"""外部输入契约闸：读侧对「别人写的文件」的路径与版本假设必须成立。

跟 `test_cross_repo_enum_contract` 分开是有理由的：那个闸比的是两份 Python
枚举，一次子进程取值就够；这里比的是**跨语言**（TS 写侧 ↔ Python 读侧）的路径
解析，和**运行时文件**的版本字段。比对方式完全不同，硬塞进一个文件会让 fail-open
边界含混——那边取不到 harness venv 该全 skip，这边读不到某个 live 文件却只该
skip 那一条。

守两件事：

## 1. obs 目录两端对称（债 A34 / B2）

生产写侧和 Stats 现在读取受管 `pulse_relay_paths/v1` 映射。
本闸验证 Relay 使用 `observabilityDir` 键，Stats 的解析器使用同一个键。
旧 `live_contract.OBS_DIR` 只供历史 replay；不能再拿当前机器的 HOME
与它比较，否则会把跨机器路径差异误判为生产故障。

## 2. 外部输入的版本判定守恒（债 B16）

读侧对别人写的 JSON 有版本假设时，必须**显式判**，不能默默按老格式解析。
实测（2026-08-10）读侧已对两个源做了 `schema_version != 1` 判定；这条闸防的是
**新增外部输入时忘了判版本**：只要某个 live 输入文件里带 version/schema_version
字段，读侧就必须在代码里对它有过一次显式判定，否则登记豁免说明为什么不判。

## fail-open / fail-closed 边界

* 读不到 live 文件（对端机、文件还没生成）→ 只跳过依赖它的那一条。
* 读到了而假设不成立 → 红。
* 源码文件读不到（不在这台机器/仓不完整）→ skip 并说清，不假绿。

## 怎么手动跑

    cd Stats/exporter && python3 -m unittest tests.test_external_input_contract -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_stats import live_contract  # noqa: E402
from pulse_stats import production_paths  # noqa: E402
from pulse_stats.live_contract import OBS_DIR  # noqa: E402

PULSE_REPO = Path(__file__).resolve().parents[3]
RELAY_OBS_TS = PULSE_REPO / "Relay/src/relay-observability.ts"
LIVE_SOURCES_PY = Path(__file__).resolve().parents[1] / "pulse_stats/live_sources.py"

#: 写侧读的 env 名。改名了这边要知道。
_OBS_ENV = re.compile(r"env\[\s*'([A-Z_]+)'\s*\]\s*\?\?\s*''")

#: 带版本字段、且**读侧真的会读**的 live 输入。key = 人话名, value = 路径属性名。
_VERSIONED_INPUTS = {
    "probe-heartbeats registry": "registry",
    "incident state": "state",
}

#: 读侧刻意不做版本判定的输入，必须带理由。
_EXEMPT_UNVERSIONED = {
    "heartbeats": (
        "Relay 写的心跳表是裸 map，从来没有版本字段；读侧靠结构判定"
        "（isinstance dict + sources 形状）而非版本号。给它加版本判定"
        "需要写侧先加字段，属跨仓改动，不是读侧单方面能做的。"
    ),
}


class ObsDirSymmetryTests(unittest.TestCase):
    """债 A34 / B2：写侧和读侧使用同一受管映射键。"""

    def _write_side_source(self) -> str:
        if not RELAY_OBS_TS.is_file():
            self.skipTest(
                f"SKIP≠PASS：读不到写侧源码 {RELAY_OBS_TS}，本条未做判定。"
            )
        return RELAY_OBS_TS.read_text(encoding="utf-8")

    def test_write_side_and_stats_share_managed_observability_key(self) -> None:
        """写侧和 Stats 从同一受管路径映射取 obs，不依赖机器 HOME。"""
        source = self._write_side_source()
        match = re.search(r"managedRelayPath\(['\"]([^'\"]+)['\"]", source)
        self.assertIsNotNone(
            match,
            f"在 {RELAY_OBS_TS.name} 里找不到受管 obs 路径入口。",
        )
        assert match is not None
        self.assertEqual(match.group(1), "observabilityDir")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path_map = root / "paths.json"
            obs = root / "obs"
            path_map.write_text(json.dumps({
                "schema": "pulse_relay_paths/v1", "site": "ownermac", "root": str(root),
                "paths": {"dataDir": str(root / "data"), "observabilityDir": str(obs)},
            }), encoding="utf-8")
            resolved, valid = production_paths.resolve(live_contract.LivePaths(), path_map)
            self.assertTrue(valid)
            self.assertEqual(resolved.obs, obs)

    def test_read_side_documents_the_env_it_ignores(self) -> None:
        """写侧认的那个 env 名，读侧必须知道自己不认它。

        读侧**刻意**不读 `RELAY_OBS_DIR`（加它是行为改动）。但「不读」必须是
        写下来的决定：常量附近要提到这个 env 名，否则下一个人根本不知道存在
        这条分叉。env 改名而读侧注释没跟 → 红。
        """
        source = self._write_side_source()
        match = _OBS_ENV.search(source)
        self.assertIsNotNone(
            match, "写侧 obs env 读取表达式找不到，比对方式需跟着改。"
        )
        assert match is not None
        env_name = match.group(1)
        contract_src = Path(live_contract.__file__).read_text(encoding="utf-8")
        self.assertIn(
            env_name, contract_src,
            f"写侧认 {env_name} 而读侧 live_contract.py 里只字未提。"
            f"设了这个 env，写侧换目录、读侧还盯 {OBS_DIR}，事件流静默断供。"
            "修法：在 OBS_DIR 旁写明「不认这个 env 及其后果」，或让读侧真的读它。",
        )


class VersionGateTests(unittest.TestCase):
    """债 B16：读侧对带版本的外部输入必须有显式版本判定。"""

    def test_versioned_inputs_are_gated_in_code(self) -> None:
        """凡是 live 文件里带版本字段的输入，读侧代码里必须判过版本。

        防的是「新接一个外部源，忘了判版本」——写侧升 v2 换了结构，读侧仍按
        v1 解析，读出来的是错的而不是空的，比断供更难发现。
        """
        if not LIVE_SOURCES_PY.is_file():
            self.skipTest(f"SKIP≠PASS：读不到 {LIVE_SOURCES_PY}，本条未做判定。")
        source = LIVE_SOURCES_PY.read_text(encoding="utf-8")
        paths = live_contract.LivePaths()

        ungated: list[str] = []
        for label, attr in _VERSIONED_INPUTS.items():
            path = getattr(paths, attr)
            if path is None or not Path(path).is_file():
                continue  # 该源在这台机器上不存在或未登记，跳过它一条，不影响其余
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            version_keys = [k for k in raw if "version" in k.lower()]
            if not version_keys:
                continue
            for key in version_keys:
                if not re.search(rf'get\(\s*["\']{re.escape(key)}["\']', source):
                    ungated.append(
                        f"{label}（{Path(path).name}）带 {key}="
                        f"{raw[key]!r}，读侧代码里没有对它的显式判定"
                    )
        self.assertEqual(
            ungated, [],
            "外部输入带版本字段但读侧不判——写侧升版会让读侧按老格式静默读错：\n  "
            + "\n  ".join(ungated),
        )

    def test_unversioned_exemptions_still_apply(self) -> None:
        """豁免的无版本输入若哪天长出版本字段，豁免理由就失效了，必须红。"""
        paths = live_contract.LivePaths()
        stale: list[str] = []
        for attr, reason in _EXEMPT_UNVERSIONED.items():
            path = Path(getattr(paths, attr))
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict) and any("version" in k.lower() for k in raw):
                stale.append(
                    f"{attr}（{path.name}）现在带版本字段了，"
                    f"豁免理由「{reason[:24]}…」已失效——请改为显式判定"
                )
        self.assertEqual(
            stale, [], "豁免过期：\n  " + "\n  ".join(stale)
        )


class HarnessAuthoritySourceContractTests(unittest.TestCase):
    def test_global_wi_metrics_read_the_registered_service_checkout(self) -> None:
        paths = live_contract.LivePaths()

        self.assertEqual(paths.work_items, paths.harness_repo / ".harness/work-items")
        self.assertEqual(paths.handoffs, paths.harness_repo / ".harness/handoffs")
        self.assertEqual(paths.policies, paths.harness_repo / ".harness/policies")
        self.assertNotEqual(
            paths.harness_repo,
            Path("/Users/owner/Coevolution"),
            "Stats must not call a dirty human checkout global authority",
        )


if __name__ == "__main__":
    unittest.main()
