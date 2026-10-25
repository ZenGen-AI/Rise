"""默认的数据生产器:模板生成情境与人格,确定性 rollout 后端,再交给既有的 produce_grouped_dataset。

**与目标维度无关(R14)。** 情境与人格来自 TemplateGenerationBackend 的固定模板池,只受
data_spec.seed 影响;rollout 是罐头话语。它让闭环能跑通,但训练数据并不针对规划器选的维度。

grpo_opd 的组内必须真有差异,否则优势恒为 0。默认 rollout 后端按 rollout 序号交替:
偶数序号的训练角色位每句都含 reward.outcome_rules 里每个分量的第一个关键词(结果分 1),
奇数序号说一句不含任何关键词的中性话(结果分 0)。所以 group_size >= 2 的每组奖励都是
{1, 0} 混合,sum 与 product 两种组合下都不退化。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.gitstore import GitStore
from mindtrainer.data_spec import DataSpec, DataSynthesisError, stages_to_synthesize
from mindtrainer.training_config import TrainingConfig
from simulator.backends import GenerationBackend, ModelBackend, ScriptedBackend, TemplateGenerationBackend
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.dataset import produce_grouped_dataset
from simulator.generators import generate_persona, generate_scenario

# (cfg, 角色位顺序, max_turns) -> produce_grouped_dataset 的 backends_for_rollout
RolloutFactory = Callable[
    [TrainingConfig, list[str], int], Callable[[Scenario, int], dict[str, ModelBackend]]
]

# 中性话语的候选:取第一句不含任何关键词的。关键词是纯子串匹配,所以要逐句核对。
_NEUTRAL_CANDIDATES = ("嗯。", "(沉默)", "...", "-", "~", "|")
_MAX_TURNS_CAP = 200


def _neutral_utterance(keywords: list[str]) -> str:
    for text in _NEUTRAL_CANDIDATES:
        if not any(kw in text for kw in keywords):
            return text
    raise DataSynthesisError(
        f"every neutral utterance {list(_NEUTRAL_CANDIDATES)} contains an outcome keyword; "
        f"the keyword rollouts cannot produce a zero-reward rollout"
    )


def keyword_alternating_rollouts(
    cfg: TrainingConfig, slots: list[str], max_turns: int
) -> Callable[[Scenario, int], dict[str, ModelBackend]]:
    """默认的 rollout 后端工厂。只对 outcome_split 归因有意义:judge 归因的分数不由关键词决定。"""
    if cfg.reward.attribution != "outcome_split":
        raise DataSynthesisError(
            f"keyword rollouts need reward.attribution 'outcome_split', got {cfg.reward.attribution!r}"
        )
    hit_words = [cfg.reward.outcome_rules[name][0] for name in cfg.reward.components]
    all_words = [w for name in cfg.reward.components for w in cfg.reward.outcome_rules[name]]
    hit = " ".join(hit_words)
    miss = _neutral_utterance(all_words)

    def backends_for_rollout(scenario: Scenario, k: int) -> dict[str, ModelBackend]:
        trained_line = hit if k % 2 == 0 else miss
        return {
            slot: ScriptedBackend(
                {slot: [trained_line if slot == cfg.trained_slot else miss] * max_turns}
            )
            for slot in slots
        }

    return backends_for_rollout


def _slot_layout(cfg: TrainingConfig, opponent_model_ref: str) -> list[tuple[str, str, str | None]]:
    """[(slot_id, model_backend, model_ref)],训练角色位在前。interact 指令自带 bindings,
    照它的角色位与对手后端;batch 指令没有 bindings,配一个 frozen_reference 的对手。"""
    if cfg.bindings:
        slots = [b.slot_id for b in cfg.bindings]
        if len(set(slots)) != len(slots):
            raise DataSynthesisError(f"bindings bind a slot more than once: {slots}")
        others = [b for b in cfg.bindings if b.slot_id != cfg.trained_slot]
        if cfg.trained_slot not in slots or not others:
            raise DataSynthesisError(
                f"bindings must bind trained_slot {cfg.trained_slot!r} "
                f"and at least one other slot, got {slots}"
            )
        return [(cfg.trained_slot, "self_play", None)] + [
            (b.slot_id, b.model_backend, b.model_ref) for b in others
        ]
    opponent = "opponent" if cfg.trained_slot != "opponent" else "opponent_b"
    return [(cfg.trained_slot, "self_play", None), (opponent, "frozen_reference", opponent_model_ref)]


class TemplateDataProducer:
    """DataProducer 的默认实现。确定性:同一份 (run_id, cfg, data_spec) 写出逐字节相同的
    episode 与清单,所以崩溃后重跑会覆盖成同样的内容(GitStore 对相同内容是空操作)。"""

    def __init__(
        self,
        *,
        rollout_factory: RolloutFactory = keyword_alternating_rollouts,
        generation_backend: GenerationBackend | None = None,
        max_turns: int = 4,
        opponent_model_ref: str = "template-opponent-v1",
    ) -> None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise TypeError(f"max_turns must be an int, got {type(max_turns).__name__}")
        if not (1 <= max_turns <= _MAX_TURNS_CAP):
            raise ValueError(f"max_turns must be in [1, {_MAX_TURNS_CAP}], got {max_turns}")
        if not isinstance(opponent_model_ref, str) or not opponent_model_ref:
            raise ValueError("opponent_model_ref must be a non-empty str")
        self._rollout_factory = rollout_factory
        self._generation = generation_backend or TemplateGenerationBackend()
        self._max_turns = max_turns
        self._opponent_model_ref = opponent_model_ref

    def _world(
        self, cfg: TrainingConfig, spec: DataSpec
    ) -> tuple[list[tuple[Scenario, RoleAssignment]], dict[str, Persona], list[str]]:
        layout = _slot_layout(cfg, self._opponent_model_ref)
        slots = [slot for slot, _, _ in layout]
        personas: dict[str, Persona] = {}
        scenarios: list[tuple[Scenario, RoleAssignment]] = []
        for i in range(spec.scenario_count):
            base = generate_scenario(self._generation, spec.seed + i, spec.purpose)
            scenario = Scenario(
                id=base.id, background=base.background,
                role_slots=[RoleSlot(slot, f"角色位 {slot}") for slot in slots],
                conflict=base.conflict, norms=list(base.norms),
                purpose=base.purpose, source=base.source,
            )
            bindings = []
            for j, (slot, backend, model_ref) in enumerate(layout):
                persona = generate_persona(self._generation, (spec.seed + i) * len(layout) + j)
                personas[persona.id] = persona
                bindings.append(RoleBinding(slot, persona.id, backend, model_ref=model_ref))
            scenarios.append(
                (scenario, RoleAssignment(scenario.id, bindings, max_turns=self._max_turns))
            )
        return scenarios, personas, slots

    def world_for_interact(
        self, cfg: TrainingConfig, data_spec: DataSpec
    ) -> tuple[list[Scenario], dict[str, Persona]]:
        """interact 生成路的情境与人格,不写树、不跑 rollout。

        interact 指令的 bindings 每个角色位一条,对所有情境共用(trajectory_source 每次 rollout 都按它
        读人格),所以人格按角色位生成一份,而不是像 _world 那样按 (情境, 角色位) 各生成一份。
        情境的 role_slots 照 bindings 的角色位顺序排;其余字段取自模板生成的基底。
        确定性:同一份 (cfg 的角色位, data_spec) 恒得到相同的 id,所以回写前崩溃、重入时
        重新生成并重写的内容与上次逐字节相同(GitStore 对相同内容是空操作)。
        返回 (情境列表, {slot_id: 人格});角色位顺序即 bindings 的顺序。"""
        slots = [b.slot_id for b in cfg.bindings]
        if not slots:
            raise DataSynthesisError("bindings is empty: an interact world needs the slot layout")
        if len(set(slots)) != len(slots):
            raise DataSynthesisError(f"bindings bind a slot more than once: {slots}")
        scenarios: list[Scenario] = []
        for i in range(data_spec.scenario_count):
            base = generate_scenario(self._generation, data_spec.seed + i, data_spec.purpose)
            scenarios.append(Scenario(
                id=base.id, background=base.background,
                role_slots=[RoleSlot(slot, f"角色位 {slot}") for slot in slots],
                conflict=base.conflict, norms=list(base.norms),
                purpose=base.purpose, source=base.source,
                opponent_script={k: list(v) for k, v in base.opponent_script.items()},
            ))
        personas = {
            slot: generate_persona(self._generation, data_spec.seed * len(slots) + j)
            for j, slot in enumerate(slots)
        }
        return scenarios, personas

    def produce(self, store: GitStore, run_id: str, cfg: TrainingConfig, data_spec: DataSpec) -> None:
        stages = stages_to_synthesize(store, run_id, cfg)
        if not stages:
            return
        scenarios, personas, slots = self._world(cfg, data_spec)
        backends_for_rollout = self._rollout_factory(cfg, slots, self._max_turns)
        for name in stages:  # required_stages 的顺序:sft 在前,grpo_opd 在后
            produce_grouped_dataset(
                store, run_id, name,
                scenarios=scenarios, personas=personas,
                backends_for_rollout=backends_for_rollout,
                group_size=data_spec.stages[name].group_size,
            )
