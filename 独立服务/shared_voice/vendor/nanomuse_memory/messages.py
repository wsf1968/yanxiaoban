"""Compatibility value used only by upstream memory.tidy's LLM call."""
from dataclasses import dataclass


@dataclass
class Message:
    role: str
    content: str

    @classmethod
    def user(cls, content):
        return cls('user', content)
