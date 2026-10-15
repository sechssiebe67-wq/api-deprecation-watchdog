"""Curated default source registry.

These are public changelog feeds used to seed a fresh deployment so the
dashboard is useful immediately. Operators can remove or replace them at any
time; seed sources can also be disabled with ``SEED_SOURCES=false``.
"""
from .models import Source

DEFAULT_SOURCES = [
    Source(
        name="GitHub Changelog",
        url="https://github.blog/changelog/feed/",
        kind="rss",
        api_name="GitHub",
        tracked_versions=["v3", "v4"],
    ),
    Source(
        name="Stripe API Changelog",
        url="https://docs.stripe.com/changelog",
        kind="html",
        api_name="Stripe",
        tracked_versions=["2023", "2024", "2025"],
    ),
    Source(
        name="OpenAI API Changelog",
        url="https://platform.openai.com/docs/changelog",
        kind="html",
        api_name="OpenAI",
        tracked_versions=["v1"],
    ),
]


def default_sources():
    return [Source(**source.to_dict()) for source in DEFAULT_SOURCES]
