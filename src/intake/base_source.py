"""Abstract base interface for renewal intake feeds."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import date
from pydantic import BaseModel, Field

class RawRenewalItem(BaseModel):
    policy_number: str
    insured_name: str
    applicant_id: str
    carrier_name: str
    line_of_business: str = "Homeowners"
    discussion_title: str = "Manual Homeowners Renewal"
    source: str = "Manual"
    expiration_date: date
    effective_date: Optional[date] = None
    expiring_premium: Optional[float] = None
    underwriter_name: Optional[str] = None
    underwriter_email: Optional[str] = None
    assigned_agent: Optional[str] = None
    portal_supported: bool = False
    portal_url: Optional[str] = None

class BaseRenewalSource(ABC):
    @abstractmethod
    def fetch_renewals(self, target_date: Optional[date] = None) -> List[RawRenewalItem]:
        """Fetches raw renewal records from the source system."""
        pass
