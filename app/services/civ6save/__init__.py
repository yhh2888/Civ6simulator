"""Read-only decoder for Civilization VI ``.Civ6Save`` files.

The save format is undocumented. This package decodes the parts that were
verified against real Gathering Storm saves (see README, section "세이브 파일
불러오기") and reports everything else as unknown instead of guessing.

Public entry point: :func:`decode_save`.
"""

from app.services.civ6save.container import SaveFormatError, read_container
from app.services.civ6save.decoder import SaveData, decode_save
from app.services.civ6save.hashes import TypeNames, type_hash

__all__ = [
    "SaveData",
    "SaveFormatError",
    "TypeNames",
    "decode_save",
    "read_container",
    "type_hash",
]
