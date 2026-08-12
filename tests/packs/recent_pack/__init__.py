"""Fixture pack: references a symbol that only exists in recent ComfyUI."""

import comfy.ldm.minimax.model as minimax

NODE_CLASS_MAPPINGS = {}


def build():
    return minimax.MiniMaxH3Model
