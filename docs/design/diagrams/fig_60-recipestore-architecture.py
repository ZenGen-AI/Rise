"""RecipeStore 的模块与依赖。只依赖 controlplane。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="RecipeStore 的模块",
        desc="七个模块分三层:两个处理器在上,检索、增益、战绩与生命周期在中间,id 与校验器在底层;"
             "整个包只导入 controlplane。")
    c.lane(14, 44, 1052, 130, "处理器:注入调和循环,签名 Callable[[GitStore, Action], None]", kind="control")
    c.lane(14, 196, 1052, 150, "领域模块:读写产物树", kind="control")
    c.lane(14, 368, 1052, 120, "纯函数与工具:不读树", kind="control")

    kw = dict(kind="control", h=74)
    admit = c.box(170, 84, 300, "admit", "handle_admit_recipe\nhas_repro_evidence", **kw)
    record = c.box(610, 84, 300, "record", "handle_record_track\nused_as_for", **kw)

    lib = c.box(40, 236, 300, "library", "find_recipes · get_recipe\ncompare · track_summary", **kw)
    track = c.box(390, 236, 300, "track", "append_track_entry · verdict\ndeprecate · deprecated_by", **kw)
    gain = c.box(740, 236, 300, "gain", "compute_gain · GainResult\nbaseline_run_of", **kw)

    ids = c.box(170, 400, 300, "ids", "recipe_id_for · run_id_of", **kw)
    val = c.box(610, 400, 300, "validate", "validate_reproducibility\nReproReport · RECIPE_TYPES", **kw)

    cp = c.box(170, 530, 740, "controlplane", "本包之外只导入它:Envelope · GitStore · paths · states · ledger · evidence · Action",
               kind="control", h=64)

    c.edge(admit, track, "bottom", "top", a_f=0.7, b_f=0.2)
    c.edge(record, track, "bottom", "top", a_f=0.2, b_f=0.8)
    c.edge(record, gain, "bottom", "top", a_f=0.7, b_f=0.4)
    c.edge(record, lib, "left", "top", a_f=0.85, b_f=0.7, via=[(580, 147), (580, 214), (250, 214)])
    c.edge(track, lib, "left", "right")
    c.edge(lib, ids, "bottom", "top", a_f=0.6, b_f=0.3, label="取 run id")
    c.note(926, 400, 136, "validate 被\nTrainScheduler\n的提名器调用", kind="neutral")

    c.legend(40, 622, [("control", "控制层模块", False)])
    return c
