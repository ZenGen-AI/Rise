"""`--interact` 的装配期检查(设计 90 §6.1 的十一条拒绝项,除第 1 条在选项层、第 10 条不可达)。

全部在第一个 tick 之前跑,出错抛 FactoryConfigError(命令行退出码 2、一行错误),什么都不写。
顺序跟设计表走;其中 4(点名要成对)与 8(情境数不少于 groups_per_step)由 `load_training_defaults` 的严格解析
一并把关,所以它们落在第 2 条之前读文件的那一步,而不是单独再判一遍。

凭证只读环境变量,错误信息只报变量名。`opponent.base_url` 带 `user:pw@` 也拒绝,而且信息里不回显地址:
地址一旦进了异常文字,就会出现在日志、指令的失败原因与回溯里。

trainscheduler 与 mindagent 惰性导入:不开 --interact 的入口不该为此多载入它们(Ruling 16)。
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Mapping

from controlplane.gitstore import GitStore
from controlplane.paths import persona_file, scenario_file
from factory.configio import CONFIG_DATA_ERRORS, describe, preflight
from factory.errors import FactoryConfigError

TRAINING_PURPOSE = "training"
_DEFAULTS = "training_defaults.yaml"


def check_interact_config(store: GitStore, environ: Mapping[str, str]):
    """跑第 2 到 9 条,返回读到的 TrainingDefaults(装配的其余步骤接着用,不重读)。"""
    from trainscheduler.config import load_training_defaults

    defaults = preflight(_DEFAULTS, lambda: load_training_defaults(store))          # 含第 4、8 条
    section = defaults.interact
    if section is None:
        raise FactoryConfigError(
            f"{_DEFAULTS} has no interact section; --interact needs it (trained_slot, slots, "
            "max_turns_per_episode, memory and either scenario_ids with persona_ids or a data_spec)"
        )
    _check_opponent(section, defaults.opponent, environ)                              # 第 3、9 条
    if section.scenario_ids:
        _check_named_world(store, section)                                            # 第 5、6、7 条
        if int(defaults.training_config.get("sft_steps", 0)) > 0:
            raise FactoryConfigError(
                f"{_DEFAULTS}: the named-scenario interact path has no data_spec to synthesize SFT data from, "
                f"so training_config.sft_steps must be 0 (it is {defaults.training_config['sft_steps']}); "
                "either drop scenario_ids and persona_ids in favour of a data_spec, or set sft_steps to 0"
            )
    return defaults


def _check_opponent(section, opponent: Mapping[str, str] | None, environ: Mapping[str, str]) -> None:
    """第 3、9 条:有对手位用 external_api 时,opponent 段在、地址干净、凭证环境变量在场。

    opponent 段写了就查地址(哪怕没有角色位用它):带 userinfo 的地址留在配置里,迟早被某条错误信息带出来。"""
    from factory.opponents import ExternalApiBackend, OpponentCredentialError, driver_for
    from simulator.datamodel import RoleBinding

    if opponent is not None and has_userinfo(opponent["base_url"]):
        raise FactoryConfigError(
            f"{_DEFAULTS}: opponent.base_url must not carry credentials (user:password@); "
            f"put the key in the environment variable named by opponent.api_key_env"
        )
    uses_external = False
    for slot in section.slots:
        if slot["slot_id"] == section.trained_slot:
            continue
        binding = RoleBinding(slot["slot_id"], "", slot["model_backend"], model_ref=slot.get("model_ref"),
                              driver=slot.get("driver"))
        try:
            uses_external = uses_external or driver_for(binding) == "external_api"
        except ValueError as exc:
            raise FactoryConfigError(f"{_DEFAULTS}: interact section: {exc}") from None
    if not uses_external:
        return
    if opponent is None:
        raise FactoryConfigError(
            f"{_DEFAULTS} has no opponent section, but an interact slot uses external_api "
            "(it needs base_url, model and api_key_env)"
        )
    name = opponent["api_key_env"]
    try:
        # 构造一次只为校验:密钥为空或不能放进请求头时抛 OpponentCredentialError(信息只有变量名)
        ExternalApiBackend(opponent["base_url"], opponent["model"], api_key=environ.get(name, ""),
                           api_key_env=name, timeout_s=1.0)
    except OpponentCredentialError as exc:
        raise FactoryConfigError(str(exc)) from None
    except ValueError:
        # 地址不是 http(s):构造器的信息会回显地址,这里不用它的文字
        raise FactoryConfigError(f"{_DEFAULTS}: opponent.base_url must be an http(s) URL with a host") from None


def has_userinfo(base_url: str) -> bool:
    """地址里带不带 user:password@。装配期检查与策略上下文每次进入时重读 opponent 段都用它:
    带着凭证的地址一旦进了请求的 host 标签,就会出现在异常文字、日志与指令的失败原因里。"""
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False                   # 形状不对的地址由后面的 http(s) 校验报,不在这里回显
    return parts.username is not None or parts.password is not None or "@" in parts.netloc


def _check_named_world(store: GitStore, section) -> None:
    """第 5、6 条:点名的情境与人格都在树里,且点名的情境 purpose 是 training。人格不受 purpose 限制。"""
    from simulator.store_adapter import read_scenario

    missing_scenarios = [sid for sid in section.scenario_ids if not store.exists(scenario_file(sid))]
    if missing_scenarios:
        raise FactoryConfigError(f"{_DEFAULTS}: scenario(s) {missing_scenarios} are not in the tree")
    missing_personas = sorted({pid for pid in section.persona_ids.values() if not store.exists(persona_file(pid))})
    if missing_personas:
        raise FactoryConfigError(f"{_DEFAULTS}: persona(s) {missing_personas} are not in the tree")
    wrong = []
    for sid in section.scenario_ids:
        try:
            purpose = read_scenario(store, sid).purpose
        except CONFIG_DATA_ERRORS as exc:          # 只转数据错误;代码的毛病原样上抛,与 preflight 同口径
            raise FactoryConfigError(f"scenario {sid}: {describe(exc)}") from None
        if purpose != TRAINING_PURPOSE:
            wrong.append(f"{sid} ({purpose})")
    if wrong:
        raise FactoryConfigError(
            f"{_DEFAULTS}: scenario(s) {', '.join(wrong)} are not purpose 'training'; training on deployment "
            "scenarios would contaminate them (the three that install-defaults writes are deployment_simulation)"
        )


def check_plan_needs_interact(store: GitStore) -> None:
    """第 11 条(没传 --interact、--enable 含 plan 时):scheduler.yaml 的 allow_interact 为真就拒绝装配。

    规划器可能写出 interact 指令,而没有 --interact 派发必然判 failed,闭环停住。这是启动时就看得出的配置矛盾。"""
    from trainscheduler.config import load_scheduler_config

    config = preflight("scheduler.yaml", lambda: load_scheduler_config(store))
    if config.allow_interact:
        raise FactoryConfigError(
            "--enable plan with scheduler.yaml allow_interact true needs --interact: the planner may write "
            "interact directives, which fail without it; either add --interact or set allow_interact to false"
        )
