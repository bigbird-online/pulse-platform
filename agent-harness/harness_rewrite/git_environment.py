"""Bind Git IO to the selected repository, not a caller's hook environment."""

import os

# Git's repository-local environment (git rev-parse --local-env-vars). Operator
# transport and credential configuration remain available across repositories.
LOCAL_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
})


def repository_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key not in LOCAL_ENV
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Only the core supplies these, e.g. its private transaction index. Never
    # remove or rewrite the caller's live index or its process environment.
    environment.update(overrides or {})
    return environment
