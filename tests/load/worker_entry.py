"""ARQ worker entry point for the perf and load tests: the production WorkerSettings, with the two
hard-coded GitHub origins pointed at local servers (src has no setting for either yet).

`API` is copied into three modules at import, so each copy is redirected; `GITHUB` is the clone
origin and git is allowed to speak file:// for it. Nothing else differs from `arq
pr_review.pipeline.queue.WorkerSettings`.
"""

import functools
import os

from pr_review.connectors.github import auth, client, posting
from pr_review.pipeline import stages
from pr_review.pipeline.queue import WorkerSettings as WorkerSettings

_api = os.environ["STUB_GITHUB_API"]
for _mod in (auth, client, posting):
    setattr(_mod, "API", _api)  # noqa: B010  (mypy: not an explicit export)
stages.GITHUB = "file://" + os.environ["STUB_GIT_ROOT"]
_ws = getattr(stages, "workspace")  # noqa: B009  (mypy: not an explicit export)
setattr(stages, "workspace", functools.partial(_ws, allow_file_protocol=True))  # noqa: B010
