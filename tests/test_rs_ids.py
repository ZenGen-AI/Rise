import pytest

from recipestore.ids import RECIPE_PREFIX, recipe_id_for, run_id_of


def test_a_recipe_id_is_the_prefix_plus_the_source_run():
    assert RECIPE_PREFIX == "recipe-"
    assert recipe_id_for("run-003") == "recipe-run-003"


def test_run_id_of_inverts_recipe_id_for():
    assert run_id_of(recipe_id_for("run-017")) == "run-017"


@pytest.mark.parametrize("bad", ["", "recipe-", "run-003", "cand-run-003", None, 3])
def test_run_id_of_refuses_anything_that_is_not_a_recipe_id(bad):
    with pytest.raises(ValueError):
        run_id_of(bad)


def test_recipe_id_for_refuses_an_empty_run_id():
    with pytest.raises(ValueError):
        recipe_id_for("")
