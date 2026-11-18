"""Standalone AstrBot BubbleFlow plugin package (bubble-flow.v1)."""

from .api import API_VERSION, PLUGIN_ID, BubbleFlowAPI, discover_bubble_flow_api
from .coordinator import (
    BubblePolicy,
    BurstBatch,
    CapturedMessage,
    SessionFlightCoordinator,
    clean_plain_bubble_text,
    default_coalesce_prompt,
    plan_outgoing_bubbles,
)

__all__ = [
    "API_VERSION",
    "PLUGIN_ID",
    "BubbleFlowAPI",
    "BubblePolicy",
    "BurstBatch",
    "CapturedMessage",
    "SessionFlightCoordinator",
    "clean_plain_bubble_text",
    "default_coalesce_prompt",
    "discover_bubble_flow_api",
    "plan_outgoing_bubbles",
]
