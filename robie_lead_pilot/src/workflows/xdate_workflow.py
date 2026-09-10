"""
X-Date Renewal Opportunities Workflow (~45, 30, and 14 days before expiration).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from src.models.cadence_models import CadenceType, ChannelType, Opportunity
from src.workflows.base_workflow import BaseWorkflow


class XDateWorkflow(BaseWorkflow):
    def __init__(self, **kwargs):
        super().__init__(cadence_type=CadenceType.XDATE_OPPORTUNITY, **kwargs)

    def calculate_first_touch_due(self, now: datetime, opportunity: Opportunity) -> datetime:
        if not opportunity.expiration_date:
            return now  # Fallback if no date specified
        exp_dt = datetime.combine(opportunity.expiration_date, datetime.min.time())
        t45 = exp_dt - timedelta(days=45)
        return t45 if t45 > now else now

    def calculate_next_touch_due(
        self, completed_touch: int, now: datetime, opportunity: Opportunity
    ) -> Optional[datetime]:
        if not opportunity.expiration_date:
            return None
        exp_dt = datetime.combine(opportunity.expiration_date, datetime.min.time())
        if completed_touch == 1:
            t30 = exp_dt - timedelta(days=30)
            return t30 if t30 > now else now + timedelta(days=1)
        elif completed_touch == 2:
            t14 = exp_dt - timedelta(days=14)
            return t14 if t14 > now else now + timedelta(days=1)
        return None

    def get_touch_channels(self, touch_number: int) -> List[ChannelType]:
        if touch_number == 0:  # T-45
            return [ChannelType.VOICE, ChannelType.EMAIL]
        elif touch_number == 1:  # T-30
            return [ChannelType.VOICE, ChannelType.EMAIL]
        elif touch_number == 2:  # T-14
            return [ChannelType.VOICE, ChannelType.SMS]
        return []
