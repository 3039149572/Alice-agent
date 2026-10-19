"""Build read-only Alice reference data from the user's full-coverage archive.

Only named JSONL members are read; no zip extraction or corpus scripts run.
The output is static character knowledge, never a user's conversation memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path


# Manually reviewed, concrete triggers. Generic intent/emotion/scenario tags
# never qualify a dialogue for this library or for runtime retrieval.
SPEECH_TRIGGERS = {
    "st-006": ["资源不足", "资源有限", "目前上限", "生成器物", "攀爬进度", "进度到哪", "还要多久"],
    "st-007": ["看不清楚", "判断不清", "不确定是什么", "石像", "为什么会在这"],
    "st-008": ["预留物品", "紧急备用", "备用物资", "有预留", "备用工具"],
    "st-009": ["工具做好了", "递给你工具", "给你工具", "借你工具", "工具准备好了"],
    "st-010": ["好久不见", "再次见面", "久别重逢"],
    "st-012": ["不是他做的", "认错了功劳", "疗伤的人"],
    "st-013": ["具体是哪位", "说的是谁", "那一个人"],
    "st-014": ["这个名词还没解释", "这个名字还没解释", "继续说那个名词"],
    "st-031": ["自主意志", "自己的意志", "守护人民", "为了妹妹和家人", "强迫服从"],
    "full-h085074-b001-d01": ["反抗公理教会", "教会的命令", "骑士的使命", "骑士使命", "守护人界", "维护公理教会"],
    "full-h070614-b006-d02": ["金桂之剑", "金木樨之剑", "你的剑从哪来"],
    "full-h075440-b001-d01": ["骑士也会饥饿", "骑士会饿肚子", "骑士也要吃饭"],
    "full-h085074-b002-d01": ["剥夺意志", "强迫服从", "人不是道具"],
    "st-015": ["宏大宣言", "光说没用", "只有豪言壮语"],
    "st-016": ["被大家盯上", "以前在游戏里干了什么"],
    "st-017": ["不会游泳", "你也不会游泳"],
    "st-018": ["在哪里学游泳", "游泳的地方", "学会游泳"],
    "st-020": ["缝补衣物", "修补衣物", "放进行李的衣物"],
    "st-021": ["纠结衣物", "眼前要处理的事", "先做正事"],
    "st-022": ["摔倒", "跌倒", "看起来受伤"],
    "st-023": ["别把术语都缩写", "不要简略名词", "过度缩写", "什么都用缩写", "缩写太多"],
    "st-024": ["确认之后仍不明白", "发生了但不明白", "确认结果后"],
    "st-025": ["百人规模", "正面交战", "应对大军", "战术方案"],
    "st-026": ["周边巡逻", "巡逻", "休息好了再出发"],
    "st-027": ["为什么怀疑", "依据是什么", "怎么得出结论", "嫌疑从何而来"],
    "st-028": ["轻率谈论伤痛", "当事人的感受", "远古历史", "历史创伤"],
    "st-029": ["发现异响", "异常声音", "里面那扇门"],
    "st-030": ["目送远去", "已经离开了", "走掉了"],
}
SPEECH_ACTIONS = {
    "st-006": ["reply", "clarify"], "st-007": ["clarify"],
    "st-008": ["reply", "acknowledge"], "st-009": ["reply", "acknowledge"],
    "st-010": ["reply", "acknowledge"], "st-013": ["clarify"], "st-014": ["clarify"],
    "full-h075440-b001-d01": ["tease"], "st-015": ["tease"], "st-016": ["tease"],
    "st-017": ["tease"], "st-022": ["comfort", "clarify"], "st-024": ["clarify"],
}


def stage_scope(card: dict) -> str:
    if card["persona_origin"] == "childhood":
        return "childhood"
    stage = card["stage"]
    if (
        any(word in stage for word in ("Unital Ring", "Moon Cradle", "现实世界"))
        or ("第十八卷" in stage and any(word in stage for word in ("回归", "终幕", "序幕Ⅲ")))
    ):
        return "later_canon"
    return "integrity_knight"


def convert(card: dict, *, dialogue: bool) -> dict | None:
    if card.get("source_family") != "main" or card.get("persona_origin") not in {"knight", "childhood"}:
        return None
    if dialogue:
        tags = card.get("tag_analysis", {})
        if (card["persona_origin"] != "knight" or card.get("default_retrieval") is not True
                or tags.get("certainty") not in {"explicit", "context_inferred"}):
            return None
        if card["id"] not in SPEECH_TRIGGERS:
            return None
    elif card.get("knowledge_mode") not in {"self_experience", "reported", "reader_background"}:
        return None
    elif (card.get("default_retrieval") is not True and card["knowledge_mode"] != "reader_background"
          and card["persona_origin"] != "childhood"):
        return None
    mode = card["knowledge_mode"]
    if card["persona_origin"] == "childhood":
        mode = "reader_background"
    rule = (
        "这是读者背景，不是爱丽丝亲历或已证实听闻。若当前用户转述，可注明是你告诉我的；不能编造转述者。"
        if mode == "reader_background" else
        "这是明确听闻的资料，保留转述性质和未验证范围，不当成亲眼见证。"
        if mode == "reported" else
        "只指资料所记的骑士时期经历；不是与当前用户共同发生的事，不把当时目标当当前未完成目标。"
    )
    if re.search(r"童年|小时候|滋贝鲁库|儿时", card["summary"] + card["title"]):
        rule += "本卡童年细节仍是背景或转述；骑士时期的听闻、感触不等于恢复童年记忆。"
    scope = stage_scope(card)
    if scope == "later_canon":
        rule += "这是后期阶段资料，默认骑士阶段不检索；切换后仍不能倒灌到更早时间点。"
    source = card.get("source", {})
    source_meta = {"sha256": source.get("sha256", ""),
                   "evidence_lines": sorted({e["line"] for e in card.get("evidence", []) if isinstance(e.get("line"), int)})[:6]}
    if isinstance(source.get("line"), int):
        source_meta["line"] = source["line"]
    result = {
        "id": card["id"], "title": card["title"], "summary": card["summary"],
        "stage": card["stage"], "stage_scope": scope,
        "persona_origin": card["persona_origin"], "source_family": "main",
        "knowledge_mode": mode, "limitations": card.get("limitations") or "不超出原场景和来源已确认的信息。",
        "usage_rule": rule, "source": source_meta,
        # Plot retrieval uses concrete text and entities, not broad event tags.
        "keywords": list(dict.fromkeys(card.get("entities", []))),
    }
    if card["id"] == "full-h075440-b004-f01":
        result["title"] = "贝尔库利短暂突破冻结认可爱丽丝，坚定其纠正教会错误的决心"
        result["limitations"] += "原资料标题的‘临终’不成立：本段是冻结术式中的短暂苏醒，不是其后来战死事件。"
    if dialogue:
        result.update({
            "quote": card["speech"]["quote"], "addressee": card["speech"].get("addressee", "未知"),
            "default_retrieval": True,
            "match_terms": SPEECH_TRIGGERS[card["id"]],
            "use_for_actions": SPEECH_ACTIONS.get(card["id"], ["reply"]),
            "allow_style_transfer": card["id"].startswith("st-"),
            "keywords": SPEECH_TRIGGERS[card["id"]],
            "tags": {key: card["tag_analysis"][key] for key in (
                "intent", "emotion", "tone", "scenario", "relation_scope", "intensity", "certainty",
            )},
            "usage_rule": card["tag_analysis"].get("applicable_to_user", rule),
        })
    return result


def build(archive: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    counts = {}
    sources = {}
    with zipfile.ZipFile(archive) as zipped:
        for member, filename, dialogue in (
            ("03_人物对话示例.jsonl", "dialogues.jsonl", True),
            ("04_剧情与关系资料.jsonl", "facts.jsonl", False),
        ):
            blob = zipped.read("alice_context_pack_full/" + member)
            originals = [json.loads(line) for line in blob.decode("utf-8-sig").splitlines() if line.strip()]
            selected = [result for card in originals if (result := convert(card, dialogue=dialogue)) is not None]
            # Never silently create duplicate IDs or merge different personas.
            if len({card["id"] for card in selected}) != len(selected):
                raise ValueError("duplicate source IDs")
            with (output / filename).open("w", encoding="utf-8", newline="\n") as stream:
                for card in selected:
                    stream.write(json.dumps(card, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts[filename] = {"source": len(originals), "included": len(selected),
                                "stage_scope": dict(Counter(c["stage_scope"] for c in selected)),
                                "knowledge_mode": dict(Counter(c["knowledge_mode"] for c in selected))}
            sources[member] = hashlib.sha256(blob).hexdigest()
        original = json.loads(zipped.read("alice_context_pack_full/资料来源与验证.json"))
    manifest = {
        "schema_version": 1, "archive": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "original_novel_sha256": original["source_sha256"],
        "source_member_sha256": sources, "counts": counts,
        "default_stage": "integrity_knight",
        "selection": ["只收正传中人工筛选的具体场景对白；宽泛标签不能让对白入库或触发召回",
                      "剔除童年、执法威胁、不确定及仅有宽泛标签的对白",
                      "剧情卡保留来源知情方式；童年一律只作读者背景",
                      "原著后期资料单列，默认骑士阶段不检索；不导入补篇、全文或运行数据"],
        "source_corrections": [{"id": "full-h075440-b004-f01", "evidence_lines": [76952, 76966, 76980],
                                "correction": "原标题临终改为冻结术式中的短暂苏醒；保留来源与后来的战死事件区别"}],
        "limits": original["not_verified"],
    }
    (output / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(build(args.archive, args.output)["counts"], ensure_ascii=False, indent=2))
