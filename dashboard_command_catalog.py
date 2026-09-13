"""Display-only command taxonomy. Workers must not use this as an execution policy."""

from dataclasses import dataclass

from automation_command_controls import (
    BEAST_SYNC, FATE_CARDS, FISHING, HUNT, INVENTORY, JOURNEY, PAGODA,
    PROFILE, STAR_FARM, TRIAL, WORLD_BOSS,
)


# Page-native operations have no Telegram text. These are display aliases,
# shared by classification and identity panel completion, not a scheduler.
IDENTITY_PAGE_COMMANDS = {
    "tianji-grind": ("miniapp:forge",),
    "star-farm-soothe": (STAR_FARM + "-soothe",),
    "star-farm-collect": (STAR_FARM + "-collect",),
    "star-farm-pull": (STAR_FARM + "-pull",),
    "beast-seek": (".寻觅灵兽",),
    "beast-release": ("miniapp:spirit-beast-release",),
    "beast-roster": (BEAST_SYNC,),
    "beast-contract": ("miniapp:spirit-beast-contract",),
    "beast-rest": ("miniapp:spirit-beast-rest",),
    "beast-abyss": ("miniapp:spirit-beast-abyss",),
    "world-collect": ("miniapp:small-world-collect",),
    "fishing": (FISHING,),
    "fishing-bait": ("miniapp:fishing-bait",),
    "fishing-chum": ("miniapp:fishing-chum",),
    "journey": (JOURNEY,),
    "pagoda": (PAGODA,),
    "treasure-hunt": (HUNT,),
    "trial": (TRIAL,),
    "fate-cards": (FATE_CARDS,),
    "world-boss": (WORLD_BOSS,),
    "inventory": (INVENTORY,),
    "profile-sync": (PROFILE,),
    "meditation-settle": ("miniapp:meditation-settle",),
    "star-farm": (STAR_FARM,),
    "star-palace-status": ("miniapp:star-palace",),
}


CATEGORIES = (("sect", "宗门"), ("realm", "境界"), ("general", "通用"))
CATEGORY_LABELS = dict(CATEGORIES)
SUBCATEGORIES = {
    "sect": ("天星宗", "星宫", "阴罗宗", "凌霄宫", "元婴宗", "太一门", "万灵宗", "合欢宗"),
    "realm": ("元婴及以上", "化神及以上", "门槛待确认"),
    "general": (
        "修炼", "侍妾", "南宫婉", "法宝", "灵兽", "垂钓", "游历", "天机日常",
        "世界活动", "宗门事务", "对战", "交易", "资料与辅助", "归属待确认", "自定义",
    ),
}
LIFECYCLE_LABELS = {
    "implemented": "已实现",
    "opt_in": "默认暂停",
    "disabled": "自动流程已关闭",
    "retired": "旧入口已停用",
}
CHANNEL_LABELS = {"miniapp": "Mini App", "group": "群指令", "local": "本地", "callback": "群按钮"}


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    label: str
    category: str
    subcategory: str
    commands: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    channel: str = "group"
    trigger: str = "定时"
    condition: str = ""
    lifecycle: str = "implemented"
    pending: bool = False

    def classification(self):
        return {
            "category": self.category,
            "category_label": CATEGORY_LABELS[self.category],
            "subcategory": self.subcategory,
            "pending": self.pending,
            "condition": self.condition,
        }


COMMAND_CATALOG = (
    CatalogEntry("destiny", "观命", "sect", "天星宗", (".观命",), channel="miniapp", trigger="每日"),
    CatalogEntry("fix-destiny", "定命", "sect", "天星宗", (".定命 <命星>",), channel="miniapp", trigger="流程"),
    CatalogEntry("divine", "推命", "sect", "天星宗", (".推命 <动作>",), channel="miniapp", trigger="流程"),
    CatalogEntry("change-destiny", "改命", "sect", "天星宗", (".改命 <动作>",), channel="miniapp", trigger="流程"),
    CatalogEntry("tianji-grind", "刷天机值", "sect", "天星宗", aliases=("刷天机值",), channel="miniapp", trigger="计划", condition="推命炼制与炼器组成的流程"),
    CatalogEntry("star-gazing", "观星", "sect", "星宫", (".观星",), trigger="事件"),
    CatalogEntry("star-shift", "改换星移", "sect", "星宫", (".改换星移 <目标>",), trigger="事件"),
    CatalogEntry("star-farm-soothe", "灵圃安抚星辰", "sect", "星宫", aliases=("宗门灵圃安抚星辰",), channel="miniapp", trigger="流程"),
    CatalogEntry("star-farm-collect", "灵圃收集精华", "sect", "星宫", aliases=("宗门灵圃收集精华",), channel="miniapp"),
    CatalogEntry("star-farm-pull", "灵圃牵引星辰", "sect", "星宫", aliases=("宗门灵圃牵引星辰",), channel="miniapp", trigger="流程"),
    CatalogEntry("star-essence", "一键收取精华", "sect", "星宫", (".一键收取精华",), channel="miniapp", trigger="流程"),
    CatalogEntry("yinluo-status", "阴罗幡状态", "sect", "阴罗宗", (".我的阴罗幡",), channel="miniapp", trigger="查询"),
    CatalogEntry("yinluo-sacrifice", "每日献祭", "sect", "阴罗宗", (".每日献祭",), channel="miniapp", trigger="每日"),
    CatalogEntry("yinluo-blood", "血洗山林", "sect", "阴罗宗", (".血洗山林",), channel="miniapp"),
    CatalogEntry("yinluo-summon", "召唤魔影", "sect", "阴罗宗", (".召唤魔影",), channel="miniapp"),
    CatalogEntry("yinluo-recall", "召回魔影", "sect", "阴罗宗", (".召回魔影",), channel="miniapp", trigger="流程"),
    CatalogEntry("yinluo-soothe", "安抚幡灵", "sect", "阴罗宗", (".一键安抚幡灵", ".安抚幡灵 <槽位>"), channel="miniapp", trigger="流程"),
    CatalogEntry("yinluo-collect", "收取精华", "sect", "阴罗宗", (".收取精华 <槽位>",), channel="miniapp", trigger="流程"),
    CatalogEntry("yinluo-imprison", "囚禁魂魄", "sect", "阴罗宗", (".囚禁魂魄 <槽位及魂魄>",), channel="miniapp", trigger="流程"),
    CatalogEntry("yinluo-convert", "化功为煞", "sect", "阴罗宗", (".化功为煞 <数量>",), channel="miniapp", trigger="流程"),
    CatalogEntry("curse-accept", "接取解咒委托", "sect", "阴罗宗", (".接取解咒委托 <编号>",), trigger="流程"),
    CatalogEntry("curse-identify", "辨认咒纹", "sect", "阴罗宗", (".辨认咒纹 <目标>",), trigger="流程"),
    CatalogEntry("curse-suppress", "借幡镇魂", "sect", "阴罗宗", (".借幡镇魂 <目标>",), trigger="流程"),
    CatalogEntry("curse-strip", "剥离咒源", "sect", "阴罗宗", (".剥离咒源 <目标>",), trigger="流程"),
    CatalogEntry("stairs", "登天阶", "sect", "凌霄宫", (".登天阶",), channel="miniapp"),
    CatalogEntry("nine-winds", "引九天罡风", "sect", "凌霄宫", (".引九天罡风",), channel="miniapp"),
    CatalogEntry("heart-platform", "问心台", "sect", "凌霄宫", (".问心台",), channel="miniapp"),
    CatalogEntry("stairs-status", "天阶状态", "sect", "凌霄宫", (".天阶状态",), channel="miniapp", trigger="查询"),
    CatalogEntry("ask-dao", "问道", "sect", "元婴宗", (".问道",), channel="miniapp"),
    CatalogEntry("guide", "引道", "sect", "太一门", (".引道 <属性>",)),
    CatalogEntry("beast-seek", "寻觅灵兽", "sect", "万灵宗", aliases=(".寻觅灵兽", "miniapp:spirit-beast-seek"), channel="miniapp"),
    CatalogEntry("beast-release", "放生灵兽", "sect", "万灵宗", aliases=("万兽谷放生",), channel="miniapp", trigger="流程", condition="只处理本轮新寻得且不符合目标的灵兽"),
    CatalogEntry("beast-roster", "万兽谷名册与体力", "sect", "万灵宗", aliases=("miniapp:spirit-beast", "miniapp:spirit-beast:xiaohao"), channel="miniapp", trigger="查询"),
    CatalogEntry("beast-contract", "灵契互动与安抚", "sect", "万灵宗", aliases=("miniapp:spirit-beast-contract",), channel="miniapp"),
    CatalogEntry("beast-rest", "灵兽休息", "sect", "万灵宗", aliases=("万兽谷休息",), channel="miniapp", trigger="流程"),
    CatalogEntry("beast-abyss", "灵兽探渊", "sect", "万灵宗", aliases=("miniapp:spirit-beast-abyss",), channel="miniapp"),
    CatalogEntry("dual-nurture", "双修温养", "sect", "合欢宗", (".双修 温养",), condition="合欢宗专属"),
    CatalogEntry("yuanying-out", "元婴出窍", "realm", "元婴及以上", (".元婴出窍",), channel="miniapp", condition="最低境界：元婴"),
    CatalogEntry("rift", "探寻裂缝", "realm", "元婴及以上", (".探寻裂缝",), condition="最低境界：元婴"),
    CatalogEntry("node", "搜寻节点", "realm", "化神及以上", (".搜寻节点",), condition="最低境界：化神"),
    CatalogEntry("world-status", "小世界", "realm", "化神及以上", (".小世界",), channel="miniapp"),
    CatalogEntry("world-manifest", "显灵", "realm", "化神及以上", (".显灵",), channel="miniapp", trigger="流程"),
    CatalogEntry("world-sermon", "神迹布道 / 赈灾", "realm", "化神及以上", (".神迹 布道", ".神迹 赈灾"), channel="miniapp"),
    CatalogEntry("world-soothe", "安抚信徒", "realm", "化神及以上", (".安抚信徒",), channel="miniapp", trigger="事件", condition="需要香火；不足时按库存与产出等待"),
    CatalogEntry("world-collect", "收割香火", "realm", "化神及以上", aliases=("小世界收割香火",), channel="miniapp", trigger="流程"),
    CatalogEntry("meditation", "闭关修炼", "general", "修炼", (".闭关修炼",), channel="miniapp"),
    CatalogEntry("deep-meditation", "深度闭关", "general", "修炼", (".深度闭关",), channel="miniapp"),
    CatalogEntry("meditation-status", "查看闭关", "general", "修炼", (".查看闭关",), channel="miniapp", trigger="查询"),
    CatalogEntry("force-exit", "强行出关", "general", "修炼", (".强行出关",), channel="miniapp", trigger="流程"),
    CatalogEntry("heqi-pill", "服用合气丹", "general", "修炼", (".服用 合气丹",), trigger="流程", condition="需要合气丹"),
    CatalogEntry("concubine-status", "我的侍妾", "general", "侍妾", (".我的侍妾",), channel="miniapp", trigger="查询"),
    CatalogEntry("concubine-place", "安置侍妾", "general", "侍妾", (".安置侍妾",), channel="miniapp", trigger="流程"),
    CatalogEntry("concubine-recall", "召回侍妾", "general", "侍妾", (".召回侍妾",), trigger="流程", condition="南陇侯交换流程内执行"),
    CatalogEntry("concubine-divine", "天机代卜", "general", "侍妾", (".天机代卜",), channel="miniapp"),
    CatalogEntry("dream", "入梦寻图", "general", "侍妾", (".入梦寻图",), channel="miniapp"),
    CatalogEntry("puzzle", "拼图", "general", "侍妾", (".拼图",), channel="miniapp", trigger="流程", condition="残图收集完成"),
    CatalogEntry("voyage", "侍妾远航", "general", "侍妾", (".侍妾远航 <路线>",)),
    CatalogEntry("voyage-return", "远航归来", "general", "侍妾", (".远航归来",), trigger="流程"),
    CatalogEntry("heart-trial", "共历心劫", "general", "侍妾", (".共历心劫", ".稳"), lifecycle="opt_in", condition="按身份单独启用"),
    CatalogEntry("falling-trial", "坠魔心劫", "general", "侍妾", (".坠魔心劫",), trigger="流程", condition="当前接在主号主魂的远航流程中"),
    CatalogEntry("concubine-search", "红尘寻缘", "general", "侍妾", (".红尘寻缘",), lifecycle="disabled"),
    CatalogEntry("concubine-dismiss", "遣散侍妾", "general", "侍妾", (".遣散侍妾",), trigger="流程", lifecycle="disabled"),
    CatalogEntry("nangong-visit", "探望南宫婉", "general", "南宫婉", (".探望南宫婉",), trigger="每日"),
    CatalogEntry("wanying", "婉影问安", "general", "南宫婉", (".婉影问安",), trigger="每日"),
    CatalogEntry("moon-meditation", "月下合参", "general", "南宫婉", (".月下合参",)),
    CatalogEntry("curse-infer", "推演封魂咒", "general", "南宫婉", (".推演封魂咒",), trigger="流程"),
    CatalogEntry("curse-protect", "护持神魂", "general", "南宫婉", (".护持神魂",), trigger="流程"),
    CatalogEntry("curse-publish", "发布解咒委托", "general", "南宫婉", (".发布解咒委托 <数量>",), trigger="流程"),
    CatalogEntry("treasure-touch", "抚摸法宝", "general", "法宝", (".抚摸法宝 <法宝>",), condition="需要对应法宝"),
    CatalogEntry("treasure-refine", "虚天鼎炼焰", "general", "法宝", (".法宝 炼焰 虚天鼎",), condition="需要虚天鼎；炼焰圆满后停止"),
    CatalogEntry("bottle-status", "掌天瓶状态", "general", "法宝", (".掌天瓶",), trigger="查询"),
    CatalogEntry("bottle-condense", "掌天瓶凝液", "general", "法宝", (".掌天瓶 凝液",), condition="需要掌天瓶"),
    CatalogEntry("bottle-nurture", "掌天瓶养树", "general", "法宝", (".掌天瓶 养树",), condition="需要掌天瓶及养树资源"),
    CatalogEntry("wings", "风雷翅加速与收尾", "general", "法宝", (".从万宝阁取下 风雷翅", ".装备 风雷翅", ".散念 风雷翅", ".上架至万宝阁 风雷翅"), trigger="流程", condition="按风雷翅设置选择身份"),
    CatalogEntry("beast-equip", "灵兽出战", "general", "灵兽", (".灵兽出战 <灵兽>",), trigger="流程", condition="宗门限制待确认", pending=True),
    CatalogEntry("beast-patrol", "灵兽巡边与归来", "general", "灵兽", (".灵兽巡边 <灵兽及路线>", ".巡边状态", ".巡边归来"), condition="宗门限制待确认；是否可执行取决于现有入口", pending=True),
    CatalogEntry("fishing", "灵溪垂钓", "general", "垂钓", aliases=("miniapp:fishing",), channel="miniapp", trigger="每日"),
    CatalogEntry("fishing-bait", "购买鱼饵", "general", "垂钓", aliases=("灵溪购买鱼饵",), channel="miniapp", trigger="流程", condition="按所选鱼饵及向下回退规则购买"),
    CatalogEntry("fishing-chum", "购买与使用窝料", "general", "垂钓", aliases=("灵溪窝料",), channel="miniapp", trigger="流程"),
    CatalogEntry("fishing-rod", "鱼竿识别与流转", "general", "垂钓", aliases=("鱼竿流转",), trigger="流程", condition="通过交易在参与身份之间流转"),
    CatalogEntry("fishing-retry", "灵溪垂钓强制重试", "general", "垂钓", aliases=("灵溪垂钓强制重试",), channel="local", trigger="手动"),
    CatalogEntry("journey", "深入历练", "general", "游历", aliases=("miniapp:journey-deep", ".游历 深入"), channel="miniapp", trigger="每日"),
    CatalogEntry("pagoda", "琉璃问心塔", "general", "游历", aliases=("琉璃问心塔",), channel="miniapp", trigger="每日"),
    CatalogEntry("treasure-hunt", "洞府寻宝", "general", "游历", aliases=(".洞府寻宝", "洞府寻宝"), channel="miniapp", trigger="每日"),
    CatalogEntry("trial", "天机试炼", "general", "天机日常", aliases=(".天机试炼",), channel="miniapp", trigger="每日"),
    CatalogEntry("fate-cards", "天机命脉", "general", "天机日常", aliases=(".天机命脉",), channel="miniapp", trigger="每日", condition="问天、翻牌、命择、任务与验命组成的流程"),
    CatalogEntry("world-boss", "青元子世界 Boss", "general", "世界活动", aliases=("青元子",), channel="miniapp", trigger="事件"),
    CatalogEntry("mulan", "支援慕兰", "general", "世界活动", (".支援慕兰 <方式>",), trigger="每日"),
    CatalogEntry("huanglong", "黄龙山报名", "general", "世界活动", (".报名黄龙山",), trigger="事件"),
    CatalogEntry("exchange", "南陇侯交换", "general", "世界活动", (".交换 <物品类型>",), trigger="事件"),
    CatalogEntry("xuangu-quiz", "玄骨答题", "general", "世界活动", (".作答 <选项>",), trigger="事件", condition="点名已启用身份且题库答案已确认"),
    CatalogEntry("merchant-look", "神秘商人查看货品", "general", "世界活动", (".查看货品",), trigger="事件"),
    CatalogEntry("merchant-buy", "神秘商人购买商品", "general", "世界活动", (".购买商品 <商品>",), trigger="流程"),
    CatalogEntry("sect-war", "宗门战况", "general", "宗门事务", (".宗门战况",), trigger="查询"),
    CatalogEntry("sect-war-join", "参战", "general", "宗门事务", (".参战",), trigger="事件"),
    CatalogEntry("sect-join", "拜入宗门", "general", "宗门事务", (".拜入宗门 <宗门>",), trigger="流程"),
    CatalogEntry("duel", "斗法", "general", "对战", (".斗法 <目标>",), trigger="计划", condition="统一轮换或一对多计划"),
    CatalogEntry("raid", "奇袭夺宝", "general", "对战", (".奇袭 夺宝",), trigger="计划"),
    CatalogEntry("trade-list", "交易上架", "general", "交易", (".上架 <物品及价格>",), trigger="流程"),
    CatalogEntry("trade-buy", "交易购买", "general", "交易", (".购买 <编号>",), trigger="流程"),
    CatalogEntry("identity-switch", "切换身份", "general", "资料与辅助", (".切换 <身份>",), trigger="流程"),
    CatalogEntry("profile", "角色资料与状态", "general", "资料与辅助", (".状态", ".我的灵根"), trigger="查询", condition="同时读取 Mini App 首页资料"),
    CatalogEntry("inventory", "储物袋刷新与查询", "general", "资料与辅助", aliases=("储物袋",), channel="miniapp", trigger="查询"),
    CatalogEntry("red-packet", "红包领取", "general", "资料与辅助", aliases=("红包领取",), channel="callback", trigger="事件"),
    CatalogEntry("custom", "自定义指令", "general", "自定义", channel="local", trigger="计划"),
    CatalogEntry("yuanying-retreat", "元婴闭关", "general", "归属待确认", (".元婴闭关",), condition="宗门与境界限制待确认", pending=True),
    CatalogEntry("second-soul-train", "第二元神修炼", "general", "归属待确认", (".元神修炼",), condition="解锁条件待确认", pending=True),
    CatalogEntry("second-soul-status", "第二元神状态", "general", "归属待确认", (".第二元神",), trigger="查询", condition="解锁条件待确认", pending=True),
    CatalogEntry("formation-start", "启阵", "general", "归属待确认", (".启阵",), condition="宗门限制待确认", pending=True),
    CatalogEntry("formation-assist", "助阵", "general", "归属待确认", (".助阵",), trigger="事件", condition="宗门限制待确认", pending=True),
    CatalogEntry("spirit-nurture", "温养器灵", "general", "法宝", (".温养器灵 <器灵>",), condition="主号主魂已停用；其他身份依原策略"),
    CatalogEntry("profile-sync", "洞府资料同步", "general", "资料与辅助", channel="miniapp", trigger="查询", condition="同步当前道号、宗门、境界和灵根"),
    CatalogEntry("meditation-settle", "出关结算", "general", "修炼", channel="miniapp", trigger="流程", condition="深度闭关完成后结算"),
    CatalogEntry("star-farm", "宗门灵圃", "sect", "星宫", channel="miniapp", condition="控制灵圃读取及安抚、收集、牵引全流程"),
    CatalogEntry("star-palace-status", "司星台状态", "sect", "星宫", channel="miniapp", trigger="查询"),
    CatalogEntry("legacy-pagoda", "旧群指令闯塔", "general", "游历", (".闯塔",), lifecycle="retired", condition="由 Mini App 琉璃问心塔替代"),
    CatalogEntry("legacy-training", "旧群指令野外历练", "general", "游历", (".野外历练 <方式>",), lifecycle="retired", condition="由 Mini App 游历替代"),
    CatalogEntry("legacy-beast", "旧群指令灵兽操作", "sect", "万灵宗", (".寻觅灵兽", ".灵兽休息 <灵兽>"), lifecycle="retired", condition="寻觅与休息已迁入 Mini App"),
    CatalogEntry("legacy-star-farm", "旧群指令观星台", "sect", "星宫", (".观星台", ".安抚星辰", ".收集精华", ".牵引星辰 <星辰>"), lifecycle="retired", condition="由 Mini App 宗门灵圃替代"),
    CatalogEntry("legacy-rollcall", "宗门点卯", "general", "宗门事务", (".宗门点卯",), lifecycle="retired"),
    CatalogEntry("legacy-gate", "借天门势", "sect", "凌霄宫", (".借天门势",), lifecycle="retired"),
    CatalogEntry("legacy-tree", "旧灵树操作", "general", "归属待确认", (".灵树灌溉", ".灵树状态", ".采摘灵果", ".协同守山"), lifecycle="retired", pending=True),
)


def _matches(command, pattern):
    prefix, placeholder, _ = pattern.partition(" <")
    return command == prefix or bool(placeholder and command.startswith(prefix + " "))


def classify_command(command, *, group="", custom=False):
    """Return metadata without changing the command, its control key or schedule."""
    normalized = " ".join(str(command or "").split())
    for entry in COMMAND_CATALOG:
        if (normalized in entry.aliases or normalized in IDENTITY_PAGE_COMMANDS.get(entry.key, ())
                or any(_matches(normalized, pattern) for pattern in entry.commands)):
            return entry.classification()
    if normalized.startswith("封魂咒链路 ["):
        entry = next(item for item in COMMAND_CATALOG if item.key == ("curse-accept" if group == "阴罗宗" else "curse-publish"))
        return entry.classification()
    subcategory = "自定义" if custom else "归属待确认"
    return {
        "category": "general",
        "category_label": "通用",
        "subcategory": subcategory,
        "pending": True,
        "condition": "归属待确认",
    }


def apply_command_classifications(panel):
    for row in panel.get("commands") or []:
        row["classification"] = classify_command(
            row.get("command"), group=row.get("group", ""), custom=bool(row.get("custom")),
        )
    return panel


def command_catalog_payload():
    entries = []
    for entry in COMMAND_CATALOG:
        entries.append({
            "id": entry.key,
            "label": entry.label,
            "commands": list(entry.commands),
            "classification": entry.classification(),
            "channel": CHANNEL_LABELS[entry.channel],
            "trigger": entry.trigger,
            "lifecycle": entry.lifecycle,
            "lifecycle_label": LIFECYCLE_LABELS[entry.lifecycle],
        })
    return {
        "categories": [{"id": key, "label": label, "subcategories": list(SUBCATEGORIES[key])} for key, label in CATEGORIES],
        "entries": entries,
    }
