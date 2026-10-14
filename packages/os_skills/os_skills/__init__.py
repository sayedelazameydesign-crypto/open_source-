"""os_skills — continuous learning: distill, store, retrieve, reuse skills."""

from os_skills.composition import (
    CompositionError,
    detect_cycle,
    skill_as_tools,
    validate_composition,
)
from os_skills.extractor import SkillExtractor
from os_skills.injector import SkillInjector, build_skill_guidance
from os_skills.manager import SkillManager, skill_tools
from os_skills.parameterize import fill_steps, parameterize_steps
from os_skills.retriever import SkillRetriever
from os_skills.store import (
    InMemorySkillStore,
    PostgresSkillStore,
    SkillStore,
    SQLiteSkillStore,
    skill_store_from_url,
)
from os_skills.types import Skill, SkillInput

__all__ = [
    "CompositionError",
    "InMemorySkillStore",
    "PostgresSkillStore",
    "SQLiteSkillStore",
    "Skill",
    "SkillExtractor",
    "SkillInjector",
    "SkillInput",
    "SkillManager",
    "SkillRetriever",
    "SkillStore",
    "build_skill_guidance",
    "detect_cycle",
    "fill_steps",
    "parameterize_steps",
    "skill_as_tools",
    "skill_store_from_url",
    "skill_tools",
    "validate_composition",
]
