"""Surfaces: how rote perceives and acts on an application.

The core (discovery, replay, handoff) depends only on the operations a surface
offers: observe, resolve a target, act, evaluate a condition, screenshot. The
web surface is the one implementation today. A desktop surface would back the
same operations with UIA/AX accessibility trees (see ``surface/desktop``).
"""
