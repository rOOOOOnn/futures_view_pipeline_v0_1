from dataclasses import dataclass, asdict, field
from typing import Optional

@dataclass
class RawResearchItem:
    company: str
    source_date: str
    source_title: str
    source_url: str
    source_type: str
    source_name: str
    content_kind: str  # full_text | pdf_text | title_only | public_reprint
    content: str
    author: Optional[str] = None
    category: Optional[str] = None
    attachments: list[str] = field(default_factory=list)
    local_files: list[str] = field(default_factory=list)
    crawl_status: str = "ok"
    notes: Optional[str] = None
    source_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)
