"""Typed failures.

The pipeline distinguishes "this stage could not run" from "the data is wrong".
The second kind must stop everything downstream, which is what PipelineHalt is for.
"""


class PipelineError(Exception):
    """Base for every error this project raises deliberately."""


class ConfigError(PipelineError):
    """config.yaml or .env is missing something required."""


class ProviderError(PipelineError):
    """A data provider failed, or returned something we refuse to trust."""


class OrderingError(ProviderError):
    """The provider ignored the ordering we asked for.

    This is the lse-data trap: `candles()` defaults to order="asc" (oldest
    first), so a caller that forgets the argument silently analyses 2003
    instead of today. We always pass order="desc" AND verify the response is
    actually descending, so a server-side default change fails loudly.
    """


class StaleDataError(ProviderError):
    """The newest row is older than the configured tolerance."""


class ContractError(PipelineError):
    """An artifact did not match its declared schema."""


class PipelineHalt(PipelineError):
    """Validation failed (or never ran) — downstream stages must not proceed."""
