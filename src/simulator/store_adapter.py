from __future__ import annotations

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    scenario_file,
    persona_file,
    episode_file,
)
from simulator.datamodel import Persona, Scenario, Episode

_NOW = "2026-09-27T00:00:00Z"


def _envelope(entity_id: str, type_: str, status: str, payload: dict, links: list[str]) -> Envelope:
    return Envelope(
        id=entity_id,
        type=type_,
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status=status,
        schema_version=1,
        links=links,
        payload=payload,
    )


def write_persona(store: GitStore, persona: Persona) -> str:
    env = _envelope(persona.id, "persona", "effective", persona.to_dict(), [])
    path = persona_file(persona.id)
    store.write_product(path, env, f"persona: {persona.id}")
    return path


def write_scenario(store: GitStore, scenario: Scenario) -> str:
    env = _envelope(scenario.id, "scenario", "effective", scenario.to_dict(), [])
    path = scenario_file(scenario.id)
    store.write_product(path, env, f"scenario: {scenario.id}")
    return path


def write_episode(store: GitStore, run_id: str, episode: Episode) -> str:
    env = _envelope(
        episode.id,
        "episode",
        "executed",
        episode.to_dict(),
        [run_id, episode.scenario_id],
    )
    path = episode_file(run_id, episode.id)
    store.write_product(path, env, f"episode: {episode.id} for {run_id}")
    return path


def read_persona(store: GitStore, persona_id: str) -> Persona:
    env = store.read_product(persona_file(persona_id))
    return Persona.from_dict(env.payload)


def read_scenario(store: GitStore, scenario_id: str) -> Scenario:
    env = store.read_product(scenario_file(scenario_id))
    return Scenario.from_dict(env.payload)


def read_episode(store: GitStore, run_id: str, episode_id: str) -> Episode:
    env = store.read_product(episode_file(run_id, episode_id))
    return Episode.from_dict(env.payload)


def list_scenarios(store: GitStore) -> list[str]:
    return store.list_products(f"{CONTROL_PLANE_ROOT}/scenarios")


def list_personas(store: GitStore) -> list[str]:
    return store.list_products(f"{CONTROL_PLANE_ROOT}/personas")
