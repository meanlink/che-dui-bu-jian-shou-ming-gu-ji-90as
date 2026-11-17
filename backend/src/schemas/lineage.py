from pydantic import BaseModel, Field
from typing import Optional


class PartInput(BaseModel):
    serial: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(pattern='^(machine|assembly|component)$')
    model: str = Field(default='', max_length=80)
    parent_hint: Optional[int] = None


class InstallInput(BaseModel):
    parent_id: int
    child_id: int
    installed_at: float
    removed_at: Optional[float] = None
    note: str = Field(default='', max_length=500)


class RemoveInput(BaseModel):
    removed_at: float
    note: str = Field(default='', max_length=500)


class RecallInput(BaseModel):
    code: str = Field(min_length=2, max_length=80)
    part_id: int
    started_at: float


class CloseInput(BaseModel):
    note: str = Field(default='', max_length=500)
    expected_version: Optional[int] = None


class ReopenInput(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


class ReleaseInput(BaseModel):
    voucher_no: str = Field(min_length=3, max_length=120)
    voucher_note: str = Field(default='', max_length=500)


class LateRelationInput(BaseModel):
    install_id: int
